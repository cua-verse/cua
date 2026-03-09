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
        from memory import MemoryStore, MemoryGetTool, MemorySearchTool, MemoryWriteTool

        memory_base = Path(os.environ.get("MEMORY_BASE_DIR", "memory_data")).resolve()
        task_id = os.environ.get("MEMORY_TASK_ID")
        self.memory_store = MemoryStore(memory_base, task_id=task_id)
        memory_search_tool = MemorySearchTool(self.memory_store)
        memory_get_tool = MemoryGetTool(self.memory_store)
        memory_write_tool = MemoryWriteTool(self.memory_store)
        print(f"TinyClaw MemoryStore initialized at: {memory_base}")

        if task_id:
            session_path = self.memory_store.init_session()
            print(f"TinyClaw session initialized: {session_path}")

        # Inject prior knowledge into instructions if available
        prior_knowledge = ""
        global_mem = self.memory_store.read_file("MEMORY.md").strip()
        if global_mem:
            prior_knowledge += (
                "\n\n## Global Memory (cross-task)\n"
                + global_mem
                + "\n\n"
            )
        if task_id:
            task_mem = self.memory_store.read_task_memory()
            if task_mem.strip():
                prior_knowledge += (
                    "## Prior Task Knowledge\n"
                    + task_mem.strip()
                    + "\n\n"
                )

        # Create agent with custom computer
        agent = ComputerAgent(
            model=self.model,
            tools=[session._computer, milestone_tool, memory_search_tool, memory_get_tool, memory_write_tool],
            only_n_most_recent_images=3,
            trajectory_dir=trajectory_dir,
            instructions=(
                prior_knowledge
                + "Use the provided computer to complete the task as described.\n\n"
                "## Memory Tools — USE IMMEDIATELY\n"
                "You have three memory tools:\n"
                "- memory_search: search memory for keywords. Returns matched lines with file/line.\n"
                "- memory_get: read a memory file (or line range) found via search.\n"
                "- memory_write: write to one of three targets:\n"
                "  - target='session': append to the current session log (timestamped, for this run only)\n"
                "  - target='task_memory': overwrite TASK_MEMORY.md — **this is the most important target**. "
                "TASK_MEMORY.md is injected word-for-word into your instructions at the start of every "
                "future run on this task. It is the PRIMARY way to pass knowledge forward across runs. "
                "Write strategies, map layouts, known pitfalls, and successful approaches here.\n"
                "  - target='memory': overwrite MEMORY.md (cross-task long-term knowledge)\n\n"
                "**Step 1 (MANDATORY):** Before doing ANYTHING else, call memory_search with keywords "
                "relevant to your task (e.g. the game name, goal, key terms). This retrieves prior "
                "knowledge that will help you avoid repeating mistakes.\n\n"
                "**Ongoing:** After every significant observation or discovery (a new screen, a "
                "failed action, a successful strategy), call memory_write with target='session' "
                "to record it. Write frequently — short notes are fine.\n\n"
                "**Periodically (every ~10 steps) and before finishing:** Call memory_write with "
                "target='task_memory' to persist a structured summary of everything you have learned "
                "so far — what worked, what failed, map layout, optimal paths, key observations. "
                "This ensures knowledge survives even if the run is cut short. Update it as you learn more; "
                "each write replaces the previous content, so always include ALL your knowledge.\n\n"
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

            # Post-run: consolidate session observations into TASK_MEMORY.md
            # The agent is instructed to do this itself, but may get cut off at
            # max_steps.  As a safety net, read the session log and append any
            # new observations so the next run benefits from this run's knowledge.
            if task_id and self.memory_store._current_session_path:
                try:
                    session_content = self.memory_store._current_session_path.read_text(
                        encoding="utf-8"
                    )
                    # Strip the header line ("# Session NNN — …")
                    observations = "\n".join(
                        ln for ln in session_content.splitlines()
                        if ln.strip() and not ln.startswith("# Session ")
                    ).strip()
                    if observations:
                        existing = self.memory_store.read_task_memory().strip()
                        session_name = self.memory_store._current_session_path.stem
                        new_section = f"\n\n## {session_name} observations\n{observations}"
                        self.memory_store.write_task_memory(
                            (existing + new_section) if existing else new_section.strip()
                        )
                        print("[TinyClaw] Consolidated session → TASK_MEMORY.md")

                        # Also consolidate cross-task observations into global MEMORY.md.
                        # Appends a task-labeled section so knowledge accumulates across tasks.
                        existing_memory = self.memory_store.read_file("MEMORY.md").strip()
                        task_section = (
                            f"\n\n## {task_id} / {session_name}\n{observations}"
                        )
                        self.memory_store.write_memory(
                            (existing_memory + task_section)
                            if existing_memory
                            else task_section.strip()
                        )
                        print("[TinyClaw] Consolidated session → MEMORY.md")
                except Exception as e:
                    print(f"[TinyClaw] Warning: session consolidation failed: {e}")

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
