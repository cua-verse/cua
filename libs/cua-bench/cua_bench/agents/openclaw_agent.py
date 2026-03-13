"""OpenClaw Agent — faithful reproduction of OpenClaw's agent-side architecture for CUA.

Adapts OpenClaw's context management (system prompt construction, compaction pipeline,
memory recall, tool loop, session persistence) to CUA's constraints:
  - instructions= for persistent context (only content never truncated)
  - Trajectory-based observation (CUA reasoning in trajectory files, not conversation)

References:
  - docs/openclaw-source-analysis.md — OpenClaw source code analysis
  - docs/openclaw-context-flow.html — interactive visual pipeline
  - openclaw/docs/concepts/ — component-level docs
  - architecture.md — AgentHLE system architecture
"""

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import register_agent
from .base import AgentResult, BaseAgent, FailureMode


def _find_latest_screenshot(trajectory_dir: Path | None) -> str:
    """Find the most recently saved screenshot_after.png in trajectory_dir.

    TrajectorySaverCallback saves one *_screenshot_after.png per computer action
    into trajectories/<trajectory_id>/turn_NNN/. The newest file corresponds to
    the action just completed.

    Returns the absolute path string, or "image:trajectory" if not found.
    """
    if not trajectory_dir or not trajectory_dir.exists():
        return "image:trajectory"
    screenshots = list(trajectory_dir.rglob("*_screenshot_after.png"))
    if not screenshots:
        return "image:trajectory"
    return str(max(screenshots, key=lambda p: p.stat().st_mtime))


def group_step_output(
    output_items: list[dict[str, Any]],
    trajectory_dir: Path | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Group a step's output items into assistant content blocks and tool results.

    CUA SDK yields multiple output items per step (text, function_call,
    computer_call, their outputs). This function batches them into two lists:
    - assistant_content: text + function_call + computer_call blocks (one assistant turn)
    - tool_results: function_call_output + computer_call_output blocks (one tool turn)

    Args:
        output_items: The result["output"] list from a CUA agent step.
        trajectory_dir: Path to trajectory directory for screenshot resolution.

    Returns:
        (assistant_content, tool_results) tuple of content block lists.
    """
    assistant_content: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []

    for item in output_items:
        item_type = item.get("type")
        if item_type == "message":
            for block in item.get("content", []):
                if block.get("text"):
                    assistant_content.append({"type": "text", "text": block["text"]})
        elif item_type == "function_call":
            assistant_content.append({
                "type": "function_call",
                "id": item.get("call_id", ""),
                "name": item.get("name", ""),
                "arguments": item.get("arguments", ""),
            })
        elif item_type == "computer_call":
            assistant_content.append({
                "type": "computer_call",
                "id": item.get("call_id", ""),
                "action": item.get("action", {}),
            })
        elif item_type == "function_call_output":
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": item.get("call_id", ""),
                "content": item.get("output", ""),
            })
        elif item_type == "computer_call_output":
            output = item.get("output", {})
            call_id = item.get("call_id", "")
            if isinstance(output, dict) and output.get("type") == "input_image":
                content_str = _find_latest_screenshot(trajectory_dir)
            else:
                content_str = str(output)[:500]
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": call_id,
                "content": content_str,
            })

    return assistant_content, tool_results

if TYPE_CHECKING:
    from ..computers import DesktopSession


@register_agent("openclaw-agent")
class OpenClawAgent(BaseAgent):
    """OpenClaw agent reproduction for CUA benchmark framework."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.model = kwargs.get("model", "anthropic/claude-sonnet-4-20250514")
        self.max_steps = kwargs.get("max_steps", 100)

    @staticmethod
    def name() -> str:
        return "openclaw-agent"

    async def perform_task(
        self,
        task_description: str,
        session: "DesktopSession",
        logging_dir: Path | None = None,
        tracer=None,
    ) -> AgentResult:
        """
        Perform a task using the CUA Computer Agent.

        Args:
            task_description: The task description/instruction
            session: The desktop session to interact with
            logging_dir: Optional directory for logging agent execution
            tracer: Optional tracer object for recording agent actions

        Returns:
            AgentResult with token counts and failure mode
        """
        try:
            from agent import ComputerAgent
        except ImportError as e:
            raise RuntimeError(
                "openclaw-agent requires the CUA `agent` package. "
                "Run: uv sync --reinstall"
            ) from e

        # Render instruction with template if provided
        instruction = self._render_instruction(task_description)

        # Create trajectory directory if logging_dir is provided
        trajectory_dir = None
        if logging_dir:
            trajectory_dir = logging_dir / "trajectories"
            trajectory_dir.mkdir(parents=True, exist_ok=True)

        from agent.tools import MilestoneTool
        from agent.tools.base import BaseTool

        milestone_tool = MilestoneTool(session.interface)

        # Build structured system prompt via PromptBuilder (US-OC-001)
        from .openclaw import (
            ContextFile,
            ContextOverflowCallback,
            MemoryGetTool,
            MemorySearchTool,
            MemoryStore,
            MemoryWriteTool,
            PromptBuilder,
            SessionManager,
            is_context_overflow_error,
        )

        # Initialize memory store (US-OC-002)
        # Derive task_id from logging_dir name or fall back to "default"
        task_id = logging_dir.parent.name if logging_dir else "default"
        memory_store = MemoryStore(task_id=task_id)
        memory_store.init_session()

        # Initialize session persistence (US-OC-004)
        session_mgr = SessionManager(task_id=task_id)
        session_mgr.init_session(model=self.model)

        # Memory tools (US-OC-003)
        memory_search = MemorySearchTool(memory_store)
        memory_get = MemoryGetTool(memory_store)
        memory_write = MemoryWriteTool(memory_store)

        tools = [session._computer, milestone_tool, memory_search, memory_get, memory_write]
        # Build tool summaries for prompt — only BaseTool instances have .name/.description
        tool_summaries = {
            tool.name: tool.description
            for tool in tools
            if isinstance(tool, BaseTool)
        }
        agents_md = (Path(__file__).parent / "openclaw" / "AGENTS.md").read_text()

        # Build context files, injecting TASK_MEMORY.md if it exists
        # Note: task description is NOT injected here — it's passed separately
        # via agent.run(instruction) to avoid duplication in context.
        context_files = [
            ContextFile(path="AGENTS.md", content=agents_md),
        ]
        bootstrap = memory_store.get_bootstrap_context()
        if bootstrap:
            context_files.append(
                ContextFile(path="TASK_MEMORY.md", content=bootstrap)
            )

        builder = PromptBuilder()
        instructions = builder.build(
            tool_summaries=tool_summaries,
            context_files=context_files,
        )

        # Context overflow detection (US-OC-005)
        # Allow env override for testing (e.g. CONTEXT_WINDOW_OVERRIDE=50000)
        import os
        ctx_override = os.environ.get("CONTEXT_WINDOW_OVERRIDE")
        overflow_cb = ContextOverflowCallback(
            model=self.model,
            context_window=int(ctx_override) if ctx_override else None,
            instructions_tokens=len(instructions) // 4,
        )

        # Create agent with custom computer
        agent = ComputerAgent(
            model=self.model,
            tools=tools,
            only_n_most_recent_images=3,
            trajectory_dir=trajectory_dir,
            instructions=instructions,
            callbacks=[overflow_cb],
        )
        print("OpenClaw Agent initialized with model:", self.model)

        # Run the agent with stop-compact-resume pattern (US-OC-006)
        # CUA SDK yields usage with input_tokens/output_tokens (OpenAI Responses API format)
        #
        # CUA's ComputerAgent.run() is an opaque async generator — we cannot inject
        # compaction summaries mid-run. Instead: break out of the loop, compact the
        # transcript, rebuild the instruction with the compaction summary, create a
        # new agent, and resume.
        try:
            total_usage = {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "response_cost": 0.0,
            }

            step = 0
            task_completed = False
            max_compactions = 3
            compaction_count = 0
            compaction_triggered = False

            while compaction_count <= max_compactions:
                compaction_triggered = False

                async for result in agent.run(instruction):
                    sys.stdout.flush()  # Flush output

                    step += 1
                    for k in total_usage:
                        total_usage[k] += result["usage"].get(k, 0)

                    # Session persistence: track step, tokens, and log messages (US-OC-004)
                    step_input = result["usage"].get("input_tokens", 0)
                    step_output = result["usage"].get("output_tokens", 0)
                    session_mgr.update_step_count(step)
                    session_mgr.update_tokens(step_input, step_output)

                    # Group step output into logical turns and log to transcript
                    assistant_content, tool_results = group_step_output(
                        result["output"], trajectory_dir
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
                        session_mgr.append_message(
                            "assistant",
                            assistant_content,
                            usage=usage,
                            stop_reason=result.get("stop_reason") or ("tool_use" if has_tools else None),
                            api="openai-responses",
                        )

                    if tool_results:
                        session_mgr.append_message("tool", tool_results)

                    # Record agent step to tracer
                    if tracer:
                        try:
                            screenshot = await session.screenshot()
                            tracer.record(
                                "agent_step",
                                {
                                    "step": step,
                                    "agent": self.name(),
                                    "model": self.model,
                                    "usage": result["usage"],
                                    "output": result["output"],
                                },
                                [screenshot],
                            )
                        except Exception as e:
                            print(f"Warning: Failed to record agent step to tracer: {e}")

                    # Proactive context overflow detection (US-OC-005 + US-OC-006)
                    if overflow_cb.needs_compaction and compaction_count < max_compactions:
                        print(f"[Compaction] Proactive trigger at step {step}")
                        compaction_triggered = True
                        break  # break async for → compact and restart

                    # Check if we've reached max_steps
                    if step >= self.max_steps:
                        print(f"\n[Max steps reached] Stopped at step {step}/{self.max_steps}")
                        break

                    # Check if task is completed (agent returned done or similar)
                    for item in result["output"]:
                        if item["type"] == "message":
                            if "DONE" in item["content"][0]["text"]:
                                print(f"\n[Task completed] Agent indicated completion at step {step}")
                                task_completed = True
                                break

                    if task_completed:
                        break

                if not compaction_triggered:
                    # agent.run() ended normally (completed, max_steps, or DONE)
                    break

                # --- Stop-Compact-Resume (US-OC-006) ---
                agent, instruction = await self._compact_and_rebuild(
                    session_mgr=session_mgr,
                    overflow_cb=overflow_cb,
                    task_description=task_description,
                    original_instructions=instructions,
                    tools=tools,
                    trajectory_dir=trajectory_dir,
                    step=step,
                    ComputerAgent=ComputerAgent,
                )
                compaction_count += 1

            print(f"\nTotal usage: {total_usage}")
            print(f"Steps completed: {step}/{self.max_steps}")
            if compaction_count > 0:
                print(f"Compactions performed: {compaction_count}")

            # Determine failure mode
            if task_completed:
                failure_mode = FailureMode.NONE
            elif step >= self.max_steps:
                failure_mode = FailureMode.MAX_STEPS_EXCEEDED
            else:
                failure_mode = FailureMode.NONE  # Completed within max_steps

            return AgentResult(
                total_input_tokens=total_usage.get("input_tokens", 0),
                total_output_tokens=total_usage.get("output_tokens", 0),
                failure_mode=failure_mode,
            )
        except Exception as e:
            # Reactive context overflow detection (US-OC-005 + US-OC-006)
            if is_context_overflow_error(str(e)) and compaction_count < max_compactions:
                overflow_cb.force_compaction()
                print(f"[ContextOverflow] API rejected — attempting reactive compaction: {e}")
                try:
                    agent, instruction = await self._compact_and_rebuild(
                        session_mgr=session_mgr,
                        overflow_cb=overflow_cb,
                        task_description=task_description,
                        original_instructions=instructions,
                        tools=tools,
                        trajectory_dir=trajectory_dir,
                        step=step,
                        ComputerAgent=ComputerAgent,
                    )
                    # One more attempt after reactive compaction
                    async for result in agent.run(instruction):
                        sys.stdout.flush()
                        step += 1
                        for k in total_usage:
                            total_usage[k] += result["usage"].get(k, 0)
                        if step >= self.max_steps:
                            break
                    return AgentResult(
                        total_input_tokens=total_usage.get("input_tokens", 0),
                        total_output_tokens=total_usage.get("output_tokens", 0),
                        failure_mode=FailureMode.MAX_STEPS_EXCEEDED if step >= self.max_steps else FailureMode.NONE,
                    )
                except Exception as retry_e:
                    print(f"[Compaction] Reactive retry also failed: {retry_e}")

            print(f"Agent execution failed: {e}")
            import traceback

            traceback.print_exc()
            return AgentResult(
                total_input_tokens=total_usage.get("input_tokens", 0),
                total_output_tokens=total_usage.get("output_tokens", 0),
                failure_mode=FailureMode.UNKNOWN,
            )

    async def _compact_and_rebuild(
        self,
        *,
        session_mgr,
        overflow_cb,
        task_description: str,
        original_instructions: str,
        tools: list,
        trajectory_dir: Path | None,
        step: int,
        ComputerAgent,
    ):
        """Run compaction on the transcript and rebuild the agent with compacted context.

        Returns (new_agent, new_instruction) for the next run cycle.
        """
        from .openclaw import compact_messages

        # Extract messages from the current run's transcript
        messages = _extract_messages_for_compaction(session_mgr)

        # Run the compaction pipeline
        compaction_result = await compact_messages(
            messages,
            self.model,
            overflow_cb.context_window,
        )

        # Find the first kept entry ID from the transcript
        history = session_mgr.load_history()
        msg_entries = [e for e in history if e.type == "message"]
        if compaction_result.first_kept_message_index < len(msg_entries):
            first_kept_id = msg_entries[compaction_result.first_kept_message_index].id
        else:
            first_kept_id = msg_entries[-1].id if msg_entries else "unknown"

        # Persist the compaction entry
        session_mgr.append_compaction(
            compaction_result.summary,
            first_kept_id,
            compaction_result.tokens_before,
        )

        # Build new instruction with compaction context
        instruction = _create_compacted_instruction(
            task_description,
            session_mgr.get_compaction_summaries(),
        )

        # Reset overflow callback and create new agent
        overflow_cb.reset_after_compaction()

        agent = ComputerAgent(
            model=self.model,
            tools=tools,
            only_n_most_recent_images=3,
            trajectory_dir=trajectory_dir,
            instructions=original_instructions,
            callbacks=[overflow_cb],
        )
        print(f"[Compaction] Agent rebuilt after compaction at step {step}")

        return agent, instruction


def _extract_messages_for_compaction(session_mgr) -> list[dict[str, Any]]:
    """Extract message entries from the transcript as dicts for compaction.

    Converts TranscriptEntry objects into the {role, content} format
    expected by the compaction pipeline.
    """
    history = session_mgr.load_history()
    messages: list[dict[str, Any]] = []
    for entry in history:
        if entry.type != "message":
            continue
        msg_data = entry.data.get("message", {})
        messages.append({
            "role": msg_data.get("role", "unknown"),
            "content": msg_data.get("content", ""),
        })
    return messages


def _create_compacted_instruction(
    task_description: str,
    compaction_summaries: list[str],
) -> str:
    """Build a new instruction string with compaction summaries prepended.

    The agent receives the compacted history as context before the task instruction.
    """
    if not compaction_summaries:
        return task_description

    summary_text = "\n\n---\n\n".join(
        f"### Compaction {i + 1}\n{s}" for i, s in enumerate(compaction_summaries)
    )
    return (
        f"## Prior Context (Compacted)\n"
        f"The following is a summary of earlier conversation history that was "
        f"compacted to save context space. Use this to maintain continuity.\n\n"
        f"{summary_text}\n\n"
        f"---\n\n"
        f"## Current Task\n{task_description}"
    )
