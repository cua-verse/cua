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

        # Run the agent and track usage
        # CUA SDK yields usage with input_tokens/output_tokens (OpenAI Responses API format)
        try:
            total_usage = {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "response_cost": 0.0,
            }

            step = 0
            task_completed = False

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
                        # Take screenshot
                        screenshot = await session.screenshot()
                        # Record the step with metadata
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

                # Proactive context overflow detection (US-OC-005)
                if overflow_cb.needs_compaction:
                    print(f"[ContextOverflow] Compaction needed at step {step}")
                    # US-OC-006 will add: await compact(session_mgr, overflow_cb, ...)

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

            print(f"\nTotal usage: {total_usage}")
            print(f"Steps completed: {step}/{self.max_steps}")

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
            # Reactive context overflow detection (US-OC-005)
            if is_context_overflow_error(str(e)):
                overflow_cb.force_compaction()
                print(f"[ContextOverflow] API rejected — overflow: {e}")
                # US-OC-006 will add retry-after-compact logic
            print(f"Agent execution failed: {e}")
            import traceback

            traceback.print_exc()
            return AgentResult(
                total_input_tokens=0,
                total_output_tokens=0,
                failure_mode=FailureMode.UNKNOWN,
            )
