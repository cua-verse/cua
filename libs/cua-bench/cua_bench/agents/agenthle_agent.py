"""AgentHLE Agent implementation using the Computer Agent SDK.
   - Add milestone tool to the agent.
   - TinyClaw memory store for cross-turn persistence.
"""

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from . import register_agent
from .base import AgentResult, BaseAgent, FailureMode

if TYPE_CHECKING:
    from ..computers import DesktopSession


@register_agent("agenthle-agent")
class AgentHLEAgent(BaseAgent):
    """Agent implementation using the CUA Computer Agent SDK."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.model = kwargs.get("model", "anthropic/claude-sonnet-4-20250514")
        self.max_steps = kwargs.get("max_steps", 100)

    @staticmethod
    def name() -> str:
        return "agenthle-agent"

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
                "agenthle-agent requires the `agenthle-agent` package to be installed. "
                "Install it with: pip install agenthle-agent"
            ) from e

        # Render instruction with template if provided
        instruction = self._render_instruction(task_description)

        # Create trajectory directory if logging_dir is provided
        trajectory_dir = None
        if logging_dir:
            trajectory_dir = logging_dir / "trajectories"
            trajectory_dir.mkdir(parents=True, exist_ok=True)

        from agent.tools import MilestoneTool
        milestone_tool = MilestoneTool(session.interface)

        # Initialize TinyClaw memory store
        from memory import MemoryStore, MemorySearchTool

        memory_base = Path(os.environ.get("MEMORY_BASE_DIR", "memory_data")).resolve()
        self.memory_store = MemoryStore(memory_base)
        memory_search_tool = MemorySearchTool(self.memory_store)
        print(f"TinyClaw MemoryStore initialized at: {memory_base}")

        # Create agent with custom computer
        agent = ComputerAgent(
            model=self.model,
            tools=[session._computer, milestone_tool, memory_search_tool],
            only_n_most_recent_images=3,
            trajectory_dir=trajectory_dir,
            instructions=(
                "Use the provided computer to complete the task as described. "
                "You have a memory_search tool — use it to recall past observations, "
                "strategies, or mistakes before making decisions. "
                "When the task is complete, indicate so clearly by outputting 'DONE'."
            ),
        )
        print("AgentHLE Agent initialized with model:", self.model)

        # Run the agent and track usage
        try:
            total_usage = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
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

                # Log step to TinyClaw memory store
                self.memory_store.append_to_daily_log(
                    f"Step {step}: tokens={total_usage['total_tokens']}"
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

            # Verify TinyClaw memory store write/read/search cycle
            log_files = self.memory_store.list_log_files()
            search_results = self.memory_store.search(["step", "tokens"])
            print(f"\n[TinyClaw] Daily logs: {log_files}")
            print(f"[TinyClaw] Search 'step tokens': {len(search_results)} results")
            if log_files:
                content = self.memory_store.read_file(log_files[-1])
                line_count = len([ln for ln in content.splitlines() if ln.strip()])
                print(f"[TinyClaw] Latest log has {line_count} non-empty lines")

            # Verify MemorySearchTool via tool.call() and log evidence
            tool_result = memory_search_tool.call({"keywords": ["step", "tokens"]})
            print(f"[TinyClaw] memory_search tool returned {len(tool_result.splitlines())} lines")
            self.memory_store.append_to_daily_log(
                f"[US-MEM-002] memory_search tool verified: "
                f"query=['step','tokens'] returned {len(tool_result.splitlines())} results. "
                f"First line: {tool_result.splitlines()[0] if tool_result.splitlines() else 'N/A'}"
            )

            # Determine failure mode
            if task_completed:
                failure_mode = FailureMode.NONE
            elif step >= self.max_steps:
                failure_mode = FailureMode.MAX_STEPS_EXCEEDED
            else:
                failure_mode = FailureMode.NONE  # Completed within max_steps

            return AgentResult(
                total_input_tokens=total_usage.get("prompt_tokens", 0),
                total_output_tokens=total_usage.get("completion_tokens", 0),
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
