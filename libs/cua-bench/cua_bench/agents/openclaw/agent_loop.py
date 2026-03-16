"""OpenClawComputerAgent — ComputerAgent subclass with mid-conversation compaction.

Overrides run() to manage a mutable message list, enabling in-place compaction
without agent rebuild. Mirrors OpenClaw's session.agent.replaceMessages() pattern
adapted for CUA's ComputerAgent lifecycle.

Design rationale (US-OC-017):
  - OpenClaw compacts via replaceMessages() within a persistent session — messages
    are swapped in-place while the agent loop continues.
  - CUA's ComputerAgent.run() uses immutable old_items + new_items lists, requiring
    a stop-compact-resume pattern (break out, rebuild agent, restart).
  - This subclass replaces that pattern by overriding run() with a mutable items list.
    When overflow_cb.needs_compaction triggers, _compact_in_place() rewrites the list
    and the loop continues — no agent rebuild needed.

Reference:
  - agent/agent.py:658-808 — parent run() lifecycle
  - openclaw/src/agents/pi-embedded-runner/compact.ts — OpenClaw compaction orchestration
  - openclaw/src/agents/compaction.ts — chunk splitting, summarization
"""

from __future__ import annotations

from typing import Any, AsyncGenerator, Callable, Dict, List, Optional

from agent.agent import ComputerAgent, get_json, get_output_call_ids
from agent.responses import replace_failed_computer_calls_with_function_calls
from litellm.responses.utils import Usage

from .context import ContextOverflowCallback, compact_messages, is_context_overflow_error
from .memory import MemoryStore
from .session import SessionManager


class OpenClawComputerAgent(ComputerAgent):
    """ComputerAgent subclass with mid-conversation compaction support.

    Overrides run() to manage a mutable message list, enabling in-place
    compaction without agent rebuild. Mirrors OpenClaw's session.agent.replaceMessages()
    pattern adapted for CUA.
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
        on_memory_flush: Callable | None = None,
        **kwargs,  # Pass through to ComputerAgent
    ):
        super().__init__(**kwargs)
        self.overflow_cb = overflow_cb
        self.session_mgr = session_mgr
        self.memory_store = memory_store
        self.summary_model = summary_model
        self.max_compactions = max_compactions
        self._compaction_count = 0
        self._on_compaction = on_compaction
        self._on_memory_flush = on_memory_flush

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

        Skips agent_config_info check since we always have a valid agent loop
        (inherited from ComputerAgent.__init__).
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
            preprocessed = await self._on_llm_start(combined)

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

            new_items += result.get("output", [])
            output_call_ids = get_output_call_ids(result.get("output", []))

            for item in result.get("output", []):
                partial_items = await self._handle_item(
                    item, self.computer_handler, ignore_call_ids=output_call_ids
                )
                new_items += partial_items
                if partial_items:
                    yield {
                        "output": partial_items,
                        "usage": Usage(
                            prompt_tokens=0,
                            completion_tokens=0,
                            total_tokens=0,
                        ).model_dump(),
                    }

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

    async def _compact_in_place(
        self,
        items: List[Dict[str, Any]],
        new_items: List[Dict[str, Any]],
    ) -> None:
        """Run compaction on the accumulated message list.

        Modifies items/new_items to contain only the compaction summary
        + kept messages. Persists compaction entry to session transcript.
        """
        # 1. Memory flush if needed
        if self._on_memory_flush:
            await self._on_memory_flush()

        # 2. Extract messages from transcript and run compaction pipeline
        all_messages = _extract_messages_for_compaction(self.session_mgr)
        compaction_result = await compact_messages(
            all_messages,
            self.summary_model,
            self.overflow_cb.context_window,
            instructions_tokens=len(self.instructions or "") // 4,
        )

        # 3. Persist compaction entry with firstKeptEntryId
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

        # 4. Rebuild items from compacted state
        # The summary becomes a user message (context), kept messages follow.
        # This matches what build_replay_messages() would produce for a resumed session.
        kept_messages = all_messages[compaction_result.first_kept_message_index:]
        compacted_items = self._build_compacted_items(
            compaction_result.summary, kept_messages
        )
        items.clear()
        items.extend(compacted_items)
        new_items.clear()

        # 5. Reset and track
        self.overflow_cb.reset_after_compaction()
        self._compaction_count += 1

        if self._on_compaction:
            self._on_compaction(self._compaction_count)

        print(
            f"[Compaction] In-place compaction #{self._compaction_count} complete "
            f"({compaction_result.tokens_before}→~{len(compacted_items)} items)"
        )

    def _build_compacted_items(
        self, summary: str, kept_messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Build a new items list from compaction output.

        Produces: [user(summary), ...kept_messages].
        The summary provides continuity context; kept messages preserve recent
        conversation verbatim.

        Args:
            summary: The compaction summary text.
            kept_messages: Messages after first_kept_message_index from the
                original message list (the recent portion preserved by compaction).
        """
        items: List[Dict[str, Any]] = []

        # Summary as context
        if summary:
            items.append({
                "role": "user",
                "content": (
                    "## Prior Context (Compacted)\n"
                    "The following is a summary of earlier conversation history that was "
                    "compacted to save context space. Use this to maintain continuity.\n\n"
                    f"{summary}"
                ),
            })

        # Kept messages (the recent portion preserved by compaction)
        if kept_messages:
            items.extend(kept_messages)

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
