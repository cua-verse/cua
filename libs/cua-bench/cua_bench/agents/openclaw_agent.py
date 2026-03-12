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
from typing import TYPE_CHECKING

from . import register_agent
from .base import AgentResult, BaseAgent, FailureMode

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
            MemoryGetTool,
            MemorySearchTool,
            MemoryStore,
            MemoryWriteTool,
            PromptBuilder,
            SessionManager,
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

        # Create agent with custom computer
        agent = ComputerAgent(
            model=self.model,
            tools=tools,
            only_n_most_recent_images=3,
            trajectory_dir=trajectory_dir,
            instructions=instructions,
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

                # Log all output to transcript (matching OpenClaw's content array format)
                step_usage = {
                    "input": step_input,
                    "output": step_output,
                    "cost": result["usage"].get("response_cost", 0),
                }
                for item in result["output"]:
                    item_type = item.get("type")
                    if item_type == "message":
                        # Assistant text — build content array from SDK output
                        content_blocks = []
                        for block in item.get("content", []):
                            content_blocks.append({
                                "type": "text",
                                "text": block.get("text", ""),
                            })
                        if content_blocks:
                            session_mgr.append_message(
                                "assistant", content_blocks,
                                usage=step_usage,
                                stop_reason=result.get("stop_reason"),
                            )
                    elif item_type == "function_call":
                        # Tool call (memory_search, memory_get, etc.)
                        session_mgr.append_message(
                            "assistant",
                            [{
                                "type": "toolCall",
                                "id": item.get("call_id", ""),
                                "name": item.get("name", ""),
                                "arguments": item.get("arguments", ""),
                            }],
                            stop_reason="tool_use",
                        )
                    elif item_type == "function_call_output":
                        # Tool result
                        session_mgr.append_message(
                            "toolResult",
                            [{"type": "text", "text": item.get("output", "")}],
                        )
                    elif item_type == "computer_call":
                        # Computer action (screenshot, click, type, etc.)
                        action = item.get("action", {})
                        session_mgr.append_message(
                            "assistant",
                            [{
                                "type": "computer_call",
                                "id": item.get("call_id", ""),
                                "action": action,
                            }],
                            stop_reason="tool_use",
                        )
                    elif item_type == "computer_call_output":
                        # Computer result (screenshot image reference)
                        # Store reference only — actual images live in trajectories
                        output = item.get("output", {})
                        output_type = output.get("type", "") if isinstance(output, dict) else ""
                        if output_type == "input_image":
                            session_mgr.append_message(
                                "toolResult",
                                [{"type": "image", "source": "trajectory"}],
                            )
                        else:
                            session_mgr.append_message(
                                "toolResult",
                                [{"type": "text", "text": str(output)[:500]}],
                            )

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
            print(f"Agent execution failed: {e}")
            import traceback

            traceback.print_exc()
            return AgentResult(
                total_input_tokens=0,
                total_output_tokens=0,
                failure_mode=FailureMode.UNKNOWN,
            )
