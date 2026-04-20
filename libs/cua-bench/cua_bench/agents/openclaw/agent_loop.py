"""OpenClawComputerAgent — ComputerAgent subclass with mid-conversation compaction.

Overrides run() to manage a mutable message list, enabling in-place compaction
without agent rebuild. Mirrors OpenClaw's session.agent.replaceMessages() pattern
adapted for CUA's ComputerAgent lifecycle.

Design rationale (US-OC-017, US-OC-028):
  - OpenClaw compacts via replaceMessages() within a persistent session — messages
    are swapped in-place while the agent loop continues.
  - CUA's ComputerAgent.run() uses immutable old_items + new_items lists, requiring
    a stop-compact-resume pattern (break out, rebuild agent, restart).
  - This subclass replaces that pattern by overriding run() with a mutable items list.
    When overflow_cb.needs_compaction triggers, _compact_in_place() rewrites the list
    and the loop continues — no agent rebuild needed.

US-OC-028 refactoring:
  - Memory flush is called pre-API (before predict_step) via _maybe_flush_memory(),
    matching OpenClaw's runMemoryFlushIfNeeded pattern in agent-runner-memory.ts.
  - Transcript logging moved into run() via _log_step_to_transcript().
  - overflow_cb auto-injected into callbacks.

Reference:
  - agent/agent.py:658-808 — parent run() lifecycle
  - openclaw/src/agents/pi-embedded-runner/compact.ts — OpenClaw compaction orchestration
  - openclaw/src/agents/compaction.ts — chunk splitting, summarization
  - openclaw/src/auto-reply/reply/agent-runner-memory.ts — memory flush pattern
"""

from __future__ import annotations

from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Union

from agent.agent import ComputerAgent, get_json, get_output_call_ids
from agent.model_config import ResolvedModel
from agent.responses import replace_failed_computer_calls_with_function_calls
from litellm.responses.utils import Usage

from .canonical import normalize_to_canonical, sanitize_items
from .context import ContextOverflowCallback, compact_messages, is_context_overflow_error
from .memory import MemoryStore
from .memory_flush import run_memory_flush
from .session import (
    MEMORY_FLUSH_PROMPT,
    MEMORY_FLUSH_SYSTEM_PROMPT,
    SILENT_REPLY_TOKEN,
    SessionManager,
    should_run_memory_flush,
)
from .subagent_registry import SubagentRegistry




class OpenClawComputerAgent(ComputerAgent):
    """ComputerAgent subclass with mid-conversation compaction support.

    Overrides run() to manage a mutable message list, enabling in-place
    compaction without agent rebuild. Mirrors OpenClaw's session.agent.replaceMessages()
    pattern adapted for CUA.

    Memory flush runs pre-API (before predict_step) via _maybe_flush_memory(),
    matching OpenClaw's single-call-site pattern (runMemoryFlushIfNeeded before
    runAgentTurnWithFallback).
    """

    def __init__(
        self,
        *,
        overflow_cb: ContextOverflowCallback,
        session_mgr: SessionManager,
        memory_store: MemoryStore,
        summary_model: str,
        max_compactions: int = 3,
        on_compaction: Callable | None = None,
        thinking_config: Optional[Any] = None,
        resolved_model: ResolvedModel | None = None,
        summary_runtime: ResolvedModel | None = None,
        registry: SubagentRegistry | None = None,
        **kwargs,  # Pass through to ComputerAgent
    ):
        # Auto-inject overflow_cb into callbacks (US-OC-028)
        callbacks = kwargs.get("callbacks", []) or []
        if overflow_cb not in callbacks:
            callbacks = [overflow_cb] + list(callbacks)
        kwargs["callbacks"] = callbacks

        super().__init__(**kwargs)
        self.overflow_cb = overflow_cb
        self.session_mgr = session_mgr
        self.memory_store = memory_store
        self.summary_model = summary_model
        self.max_compactions = max_compactions
        self._compaction_count = 0
        self._on_compaction = on_compaction
        self._last_screenshot_path: str | None = None
        # Thinking config for per-call-site params (US-OC-019/020)
        self.thinking_config = thinking_config
        self.resolved_model = resolved_model
        self.summary_runtime = summary_runtime
        self._registry = registry

    @property
    def compaction_count(self) -> int:
        """Number of compactions performed so far in this run."""
        return self._compaction_count

    async def run(
        self,
        messages,
        stream: bool = False,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
        **additional_generation_kwargs,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Override ComputerAgent.run() with mutable message list + compaction.

        Replicates the full CUA run() lifecycle (callbacks, tool execution)
        but manages items in a mutable list. When overflow_cb.needs_compaction
        triggers, compacts messages in-place and continues — no agent rebuild.

        Per-step flow (US-OC-028):
          should_continue check
          preprocessed = _on_llm_start()  <- updates current_tokens
          _maybe_flush_memory()            <- PRE-API: uses fresh current_tokens
          predict_step()                   <- API call
          yield result
          _log_step_to_transcript()        <- transcript logging
          _handle_item()
          compaction check
        """
        # Same initialization as parent run()
        if not self.agent_config_info:
            raise ValueError("Agent configuration not found")

        capabilities = self.get_capabilities()
        if "step" not in capabilities:
            raise ValueError(
                f"Agent loop {self.agent_config_info.agent_class.__name__} "
                "does not support step predictions"
            )

        await self._initialize_computers()

        # Merge kwargs and thread api credentials
        merged_kwargs = {**self.kwargs, **additional_generation_kwargs}
        if (api_key is not None) or (self.api_key is not None):
            merged_kwargs["api_key"] = api_key if api_key is not None else self.api_key
        if (api_base is not None) or (self.api_base is not None):
            merged_kwargs["api_base"] = api_base if api_base is not None else self.api_base

        # MUTABLE items list — the key difference from parent run()
        items = self._process_input(messages)
        new_items: List[Dict[str, Any]] = []

        run_kwargs = {
            "messages": messages,
            "stream": stream,
            "model": self.model,
            "agent_loop": self.agent_config_info.agent_class.__name__,
            **merged_kwargs,
        }
        await self._on_run_start(run_kwargs, items)

        while new_items[-1].get("role") != "assistant" if new_items else True:
            should_continue = await self._on_run_continue(run_kwargs, items, new_items)
            if not should_continue:
                break

            combined = items + new_items
            combined = replace_failed_computer_calls_with_function_calls(combined)
            combined = self._sanitize_runtime_messages(combined)
            preprocessed = await self._on_llm_start(combined)

            # PRE-API memory flush (US-OC-028) — runs after _on_llm_start updates
            # current_tokens, before predict_step. Matches OpenClaw's single call
            # site: runMemoryFlushIfNeeded before runAgentTurnWithFallback.
            await self._maybe_flush_memory()

            loop_kwargs = {
                "messages": preprocessed,
                "model": self.model,
                "tools": self.tool_schemas,
                "stream": False,
                "computer_handler": self.computer_handler,
                "max_retries": self.max_retries,
                "use_prompt_caching": self.use_prompt_caching,
                **merged_kwargs,
            }

            # === REACTIVE OVERFLOW: try/except around predict_step ===
            try:
                result = await self.agent_loop.predict_step(
                    **loop_kwargs,
                    _on_api_start=self._on_api_start,
                    _on_api_end=self._on_api_end,
                    _on_usage=self._on_usage,
                    _on_screenshot=self._on_screenshot,
                )
            except Exception as e:
                if (
                    is_context_overflow_error(str(e))
                    and self._compaction_count < self.max_compactions
                ):
                    self.overflow_cb.force_compaction()
                    print(f"[ContextOverflow] API rejected — reactive compaction: {e}")
                    await self._compact_in_place(items, new_items)
                    items = items + new_items
                    new_items = []
                    continue
                raise

            result = get_json(result)
            result["output"] = await self._on_llm_end(result.get("output", []))
            await self._on_responses(loop_kwargs, result)

            yield result

            # Log model-emitted assistant content/tool calls to transcript.
            self._log_step_to_transcript(result)

            new_items += result.get("output", [])
            output_call_ids = get_output_call_ids(result.get("output", []))

            for item in result.get("output", []):
                partial_items = await self._handle_item(
                    item, self.computer_handler, ignore_call_ids=output_call_ids
                )
                new_items += partial_items
                if partial_items:
                    self._log_partial_items_to_transcript(partial_items)
                if partial_items:
                    yield {
                        "output": partial_items,
                        "usage": Usage(
                            prompt_tokens=0,
                            completion_tokens=0,
                            total_tokens=0,
                        ).model_dump(),
                    }

            # === SUBAGENT COMPLETION DRAIN (US-SUB-005) ===
            # Drain before the compaction check so any new user messages from
            # completed general subagents count toward this iteration's token
            # pressure.
            self._drain_completions(new_items)
            # === POST-DELEGATION SCREENSHOT DRAIN (US-SUB-006) ===
            # Runs after completions so the GUI-delegation screenshot is the
            # freshest user turn heading into the next predict_step.
            self._drain_post_delegation(new_items)

            # === PROACTIVE COMPACTION INJECTION POINT ===
            if (
                self.overflow_cb.needs_compaction
                and self._compaction_count < self.max_compactions
            ):
                print("[Compaction] Proactive trigger — compacting in-place")
                await self._compact_in_place(items, new_items)
                items = items + new_items
                new_items = []
                continue

        await self._on_run_end(loop_kwargs, items, new_items)

    async def _maybe_flush_memory(self) -> None:
        """Run memory flush if token threshold is exceeded.

        Single call site for memory flush — called pre-API in run().
        Matches OpenClaw's runMemoryFlushIfNeeded pattern.
        """
        if self.session_mgr._state is None:
            return
        if not should_run_memory_flush(
            self.session_mgr._state,
            current_tokens=self.overflow_cb.current_tokens,
            context_window=self.overflow_cb.context_window,
        ):
            return
        await run_memory_flush(
            summary_model=self.summary_model,
            session_mgr=self.session_mgr,
            memory_store=self.memory_store,
            flush_prompt=MEMORY_FLUSH_PROMPT,
            flush_system_prompt=MEMORY_FLUSH_SYSTEM_PROMPT,
            silent_token=SILENT_REPLY_TOKEN,
            thinking_params=(
                self.thinking_config.flush_params(
                    self.summary_model,
                    runtime=self.summary_runtime,
                )
                if self.thinking_config is not None
                else None
            ),
            summary_runtime=self.summary_runtime,
        )

    def _drain_completions(self, new_items: List[Dict[str, Any]]) -> None:
        """Drain the subagent registry's completion queue into ``new_items``.

        Each completed/failed general subagent run is appended as a user
        message in ``[Subagent Result]`` format so the next LLM turn sees
        it verbatim. No-op when no registry is wired (tests, legacy paths)
        or when the queue is empty. FIFO ordering is preserved because
        ``registry.drain_completions`` drains with sequential
        ``get_nowait()`` calls.
        """
        if self._registry is None:
            return
        for run in self._registry.drain_completions():
            status = run.status.value
            body = run.result_text or run.error_message or ""
            content = (
                f"[Subagent Result] task: {run.task}\n"
                f"Status: {status}\n\n"
                f"{body}"
            )
            new_items.append({"role": "user", "content": content})

    def _drain_post_delegation(self, new_items: List[Dict[str, Any]]) -> None:
        """Drain the registry's post-delegation queue into ``new_items``.

        Messages are pushed by ``DelegateGUITool`` after a blocking GUI
        subagent run completes (US-SUB-006). They carry a fresh VM
        screenshot as ``{role: user, content: [text, image_url]}`` so the
        main agent's next ``predict_step`` sees the updated state. Pre-built
        shapes — extend verbatim, do not reformat.
        """
        if self._registry is None:
            return
        new_items.extend(self._registry.drain_post_delegation())

    # --- Screenshot path injection (US-OC-034) ---

    async def _on_screenshot(
        self, screenshot: Union[str, bytes], name: str = "screenshot"
    ) -> None:
        """Override to capture screenshot path from TrajectorySaverCallback.

        After super() dispatches to all callbacks (TrajectorySaverCallback saves
        the file to disk), we read the path from the callback's internal state.
        Callback ordering is guaranteed: TrajectorySaverCallback is auto-added
        by ComputerAgent.__init__() before our callbacks.
        """
        await super()._on_screenshot(screenshot, name)
        self._last_screenshot_path = self._resolve_screenshot_path(name)

    async def _handle_item(
        self, item: Dict[str, Any], computer=None, ignore_call_ids=None
    ) -> List[Dict[str, Any]]:
        """Override to inject screenshot path into computer call results.

        After the parent builds [computer_call_output], appends a user message
        with the local file path so the agent can reference it later (e.g. via
        analyze_image after compaction removes the base64 image).
        """
        self._last_screenshot_path = None
        result = await super()._handle_item(item, computer, ignore_call_ids)
        if self._last_screenshot_path and result:
            result.append({
                "type": "message",
                "role": "user",
                "content": f"[Screenshot saved to: {self._last_screenshot_path}]",
            })
            self._last_screenshot_path = None
        return result

    def _resolve_screenshot_path(self, name: str) -> str | None:
        """Get the file path where TrajectorySaverCallback just saved a screenshot.

        Returns None when trajectory_dir is not set (no TrajectorySaverCallback
        exists) — graceful degradation per US-OC-034 acceptance criteria.

        Note: depends on TrajectorySaverCallback private API (_get_turn_dir,
        current_artifact). Pinned via CUA submodule version.
        """
        from agent.callbacks.trajectory_saver import TrajectorySaverCallback

        for cb in self.callbacks:
            if isinstance(cb, TrajectorySaverCallback) and cb.trajectory_id:
                turn_dir = cb._get_turn_dir()
                idx = cb.current_artifact - 1  # just incremented by _save_artifact
                return str(turn_dir / f"{idx:04d}_{name}.png")
        return None

    def _log_step_to_transcript(self, result: Dict[str, Any]) -> None:
        """Log a step's output to the session transcript.

        Groups output into assistant/tool turns and appends to transcript.
        Moved from perform_task() to run() (US-OC-028).
        """
        from .transcript import group_step_output

        step_input = result["usage"].get("input_tokens", 0)
        step_output = result["usage"].get("output_tokens", 0)

        assistant_content, tool_results = group_step_output(
            result["output"], self.trajectory_dir
        )

        if assistant_content:
            has_tools = any(
                b["type"] in ("function_call", "computer_call")
                for b in assistant_content
            )
            usage = {
                "input": step_input,
                "output": step_output,
                "total": step_input + step_output,
                "cost": result["usage"].get("response_cost", 0),
            }
            self.session_mgr.append_message(
                "assistant",
                assistant_content,
                usage=usage,
                stop_reason=result.get("stop_reason") or ("tool_use" if has_tools else None),
                api=(
                    self.resolved_model.transcript_api_label
                    if self.resolved_model is not None
                    else None
                ),
            )

        if tool_results:
            self.session_mgr.append_message("tool", tool_results)

    def _log_partial_items_to_transcript(
        self,
        output_items: List[Dict[str, Any]],
    ) -> None:
        """Log tool execution outputs emitted after `_handle_item()`.

        The main model result logs assistant text/tool calls. Actual tool outputs
        (`function_call_output`, `computer_call_output`) are yielded later via
        `partial_items`, so they must be appended separately or the transcript
        loses call/result pairing.
        """
        from .transcript import group_step_output

        assistant_content, tool_results = group_step_output(
            output_items, self.trajectory_dir
        )

        # Post-tool helper messages can inject a local screenshot path as a user
        # message. Persist that as user content instead of misclassifying it as
        # assistant text.
        for item in output_items:
            if item.get("type") != "message" or item.get("role") != "user":
                continue
            content = item.get("content", "")
            if isinstance(content, str):
                self.session_mgr.append_message("user", content)

        if assistant_content:
            self.session_mgr.append_message("assistant", assistant_content)
        if tool_results:
            self.session_mgr.append_message("tool", tool_results)

    async def _compact_in_place(
        self,
        items: List[Dict[str, Any]],
        new_items: List[Dict[str, Any]],
    ) -> None:
        """Run compaction on the accumulated message list.

        Modifies items/new_items to contain only the compaction summary
        + kept messages. Persists compaction entry to session transcript.

        Memory flush runs pre-API via _maybe_flush_memory(), not here.
        If compaction fires without a prior flush, log a warning (edge case
        where token estimation missed the threshold).
        """
        # Warn if no flush preceded this compaction (token estimation edge case)
        from .session import has_already_flushed_for_current_compaction
        if self.session_mgr._state is not None and not has_already_flushed_for_current_compaction(
            self.session_mgr._state
        ):
            print("[Compaction] Warning: compaction running without prior memory flush")

        # Extract messages from transcript and run compaction pipeline
        all_messages = _extract_messages_for_compaction(self.session_mgr)
        compaction_result = await compact_messages(
            all_messages,
            self.summary_model,
            self.overflow_cb.context_window,
            instructions_tokens=len(self.instructions or "") // 4,
            thinking_params=(
                self.thinking_config.compaction_params(
                    self.summary_model,
                    runtime=self.summary_runtime,
                )
                if self.thinking_config is not None
                else None
            ),
            summary_runtime=self.summary_runtime,
        )

        # Persist compaction entry with firstKeptEntryId
        history = self.session_mgr.load_history()
        msg_entries = [e for e in history if e.type == "message"]
        if compaction_result.first_kept_message_index < len(msg_entries):
            first_kept_id = msg_entries[compaction_result.first_kept_message_index].id
        else:
            first_kept_id = msg_entries[-1].id if msg_entries else "unknown"

        self.session_mgr.append_compaction(
            compaction_result.summary,
            first_kept_id,
            compaction_result.tokens_before,
        )

        # Rebuild items from compacted state
        kept_messages = all_messages[compaction_result.first_kept_message_index:]
        canonical_messages = self._build_compacted_items(
            compaction_result.summary, kept_messages
        )

        # Convert canonical → provider-specific format via the same model-aware
        # sanitize pipeline used on the normal per-turn send path.
        compacted_items = sanitize_items(
            canonical_messages,
            model=self.resolved_model or self.model,
        )

        items.clear()
        items.extend(compacted_items)
        new_items.clear()

        # Reset and track
        self.overflow_cb.reset_after_compaction()
        self._compaction_count += 1

        if self._on_compaction:
            self._on_compaction(self._compaction_count)

        print(
            f"[Compaction] In-place compaction #{self._compaction_count} complete "
            f"({compaction_result.tokens_before}->~{len(compacted_items)} items)"
        )

    def _sanitize_runtime_messages(
        self,
        messages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Normalize and sanitize outgoing history using the live model policy.

        This mirrors OpenClaw's runtime send-path invariant: policy resolves
        from the actual model, not just the adapter target, and the same
        sanitize pipeline applies on normal turns and compaction rebuilds.
        """
        # Live in-run history is already in provider-native flat item format for
        # OpenAI/Responses loops. Re-sanitizing that stream would downgrade
        # computer_call/computer_call_output into text and cause the model to
        # repeatedly request fresh screenshots.
        if any(
            isinstance(message, dict)
            and "type" in message
            and "role" not in message
            for message in messages
        ):
            return messages

        canonical_messages = normalize_to_canonical(messages)
        return sanitize_items(
            canonical_messages,
            model=self.resolved_model or self.model,
        )

    def _build_compacted_items(
        self, summary: str, kept_messages: List[Dict[str, Any]]
    ) -> list:
        """Build a canonical message list from compaction output.

        Produces: [user(CompactionSummaryBlock), ...normalized_kept].
        The summary provides continuity context; kept messages preserve recent
        conversation verbatim in canonical form.

        Repair runs on untyped dicts BEFORE canonical normalization — the
        existing algorithm uses stop_reason at the message level and is
        well-tested.  The sanitize_items() pipeline (US-OC-039) then applies
        canonical-level repair, ordering, and format conversion.

        Args:
            summary: The compaction summary text.
            kept_messages: Messages after first_kept_message_index from the
                original message list (the recent portion preserved by compaction).

        Returns:
            Typed canonical messages (US-OC-038).  The caller
            (``_compact_in_place``) runs ``sanitize_items()`` to convert
            to provider-specific format.
        """
        from .canonical import (
            CanonicalMessage,
            CompactionSummaryBlock,
            normalize_to_canonical,
        )

        items: List[CanonicalMessage] = []

        # Summary as canonical message with CompactionSummaryBlock
        if summary:
            items.append(CanonicalMessage(
                role="user",
                content=[CompactionSummaryBlock(type="compaction_summary", text=summary)],
            ))

        # Kept messages: repair (role-based dicts) then normalize to canonical
        if kept_messages:
            from .context import repair_tool_use_result_pairing
            repair_result = repair_tool_use_result_pairing(kept_messages)
            items.extend(normalize_to_canonical(repair_result.messages))

        # Note: trailing-assistant check moved to ensure_valid_ordering()
        # in the sanitize_items() pipeline (US-OC-039).

        return items


def _extract_messages_for_compaction(session_mgr: SessionManager) -> list[dict[str, Any]]:
    """Extract message entries from the transcript as dicts for compaction.

    Converts TranscriptEntry objects into the {role, content, stop_reason} format
    expected by the compaction pipeline. Propagates stop_reason from transcript
    entries so repair_tool_use_result_pairing() can skip synthesis for
    error/aborted turns (US-OC-013).
    """
    history = session_mgr.load_history()
    messages: list[dict[str, Any]] = []
    for entry in history:
        if entry.type != "message":
            continue
        msg_data = entry.data.get("message", {})
        msg: dict[str, Any] = {
            "role": msg_data.get("role", "unknown"),
            "content": msg_data.get("content", ""),
        }
        stop_reason = msg_data.get("stop_reason")
        if stop_reason:
            msg["stop_reason"] = stop_reason
        messages.append(msg)
    return messages
