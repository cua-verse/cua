"""AgentHLE Agent implementation using the Computer Agent SDK.
   - Add milestone tool to the agent.
   - TinyClaw memory store for cross-turn persistence.
   - Planner LLM extracts observations from agent reasoning (agent is read-only for memory).
"""

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from . import register_agent
from .base import AgentResult, BaseAgent, FailureMode

if TYPE_CHECKING:
    from ..computers import DesktopSession

# How often the planner summarizes accumulated reasoning into the session log.
_PLANNER_FLUSH_INTERVAL = 10


def _extract_reasoning(result: dict) -> list[str]:
    """Extract reasoning summary texts from a single agent step result."""
    texts: list[str] = []
    for item in result.get("output", []):
        if item.get("type") == "reasoning":
            for s in item.get("summary", []):
                if s.get("type") == "summary_text" and s.get("text"):
                    texts.append(s["text"])
    return texts


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

    async def _flush_reasoning_to_session(
        self, reasoning_buffer: list[str], step: int
    ) -> None:
        """Call planner LLM to summarize reasoning buffer and write to session log."""
        if not reasoning_buffer:
            return

        from memory import call_planner

        numbered = "\n".join(
            f"- Step {step - len(reasoning_buffer) + i + 1}: {r}"
            for i, r in enumerate(reasoning_buffer)
        )

        try:
            summary = await call_planner(
                system_prompt=(
                    "You are an observation extractor. Given a sequence of agent reasoning "
                    "summaries from a computer-use session, extract the key observations: "
                    "what the agent saw, what actions succeeded or failed, what it learned "
                    "about the environment. Output a concise bulleted list. Omit routine "
                    "navigation (clicking, scrolling) unless it revealed something new."
                ),
                user_prompt=numbered,
            )
            if summary and summary.strip():
                self.memory_store.append_to_session_log(summary.strip())
                print(f"[TinyClaw] Planner flushed observations at step {step}")
        except Exception as e:
            # Planner failure is non-fatal — log raw reasoning as fallback
            print(f"[TinyClaw] Planner flush failed ({e}), writing raw reasoning")
            self.memory_store.append_to_session_log(numbered)

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

        # Initialize TinyClaw memory store (agent has read-only access;
        # planner LLM handles all writes via _flush_reasoning_to_session)
        from memory import MemoryStore, MemoryGetTool, MemorySearchTool

        memory_base = Path(os.environ.get("MEMORY_BASE_DIR", "memory_data")).resolve()
        task_id = os.environ.get("MEMORY_TASK_ID")
        if not task_id and logging_dir:
            # Auto-derive from logging_dir: {output_dir}/task_N_agent_logs → output_dir.name
            task_id = logging_dir.parent.name
            print(f"[TinyClaw] Auto-derived task_id from logging_dir: {task_id}")
        if not task_id:
            raise RuntimeError(
                "MEMORY_TASK_ID env var not set and cannot be inferred from logging_dir. "
                "Set MEMORY_TASK_ID or provide a logging_dir to enable task-scoped memory."
            )
        self.memory_store = MemoryStore(memory_base, task_id=task_id)
        memory_search_tool = MemorySearchTool(self.memory_store)
        memory_get_tool = MemoryGetTool(self.memory_store)
        print(f"TinyClaw MemoryStore initialized at: {memory_base}")

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

        # Create agent with read-only memory tools (no memory_write).
        # The planner LLM handles all memory writes by summarizing the
        # agent's reasoning every _PLANNER_FLUSH_INTERVAL steps.
        agent = ComputerAgent(
            model=self.model,
            tools=[session._computer, milestone_tool, memory_search_tool, memory_get_tool],
            only_n_most_recent_images=3,
            trajectory_dir=trajectory_dir,
            instructions=(
                prior_knowledge
                + "Use the provided computer to complete the task as described.\n\n"
                "## Memory Tools\n"
                "You have two read-only memory tools:\n"
                "- memory_search: search memory for keywords. Returns matched lines with file/line/score.\n"
                "- memory_get: read a specific memory file (or line range) found via search.\n\n"
                "**Step 1 (MANDATORY):** Before doing ANYTHING else, call memory_search with keywords "
                "relevant to your task (e.g. the game name, goal, key terms). This retrieves prior "
                "knowledge that will help you avoid repeating mistakes.\n\n"
                "Your observations are recorded automatically — focus on completing the task.\n\n"
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
            reasoning_buffer: list[str] = []

            async for result in agent.run(instruction):
                sys.stdout.flush()  # Flush output

                step += 1
                for k in total_usage:
                    total_usage[k] += result["usage"].get(k, 0)

                # Collect reasoning from this step
                reasoning_buffer.extend(_extract_reasoning(result))

                # Planner flush: summarize reasoning buffer every N steps
                if step % _PLANNER_FLUSH_INTERVAL == 0 and reasoning_buffer:
                    await self._flush_reasoning_to_session(reasoning_buffer, step)
                    reasoning_buffer.clear()

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

            # Flush any remaining reasoning
            if reasoning_buffer:
                await self._flush_reasoning_to_session(reasoning_buffer, step)
                reasoning_buffer.clear()

            print(f"\nTotal usage: {total_usage}")
            print(f"Steps completed: {step}/{self.max_steps}")

            # Post-run: use planner to consolidate session → TASK_MEMORY.md
            if task_id and self.memory_store._current_session_path:
                await self._consolidate_session(task_id)

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

    async def _consolidate_session(self, task_id: str) -> None:
        """Post-run: planner merges session log into TASK_MEMORY.md and MEMORY.md.

        Falls back to naive append if planner call fails.
        """
        from memory import call_planner

        try:
            session_content = self.memory_store._current_session_path.read_text(
                encoding="utf-8"
            )
            # Strip the header line ("# Session NNN — …")
            observations = "\n".join(
                ln for ln in session_content.splitlines()
                if ln.strip() and not ln.startswith("# Session ")
            ).strip()
            if not observations:
                print("[TinyClaw] No session observations to consolidate")
                return

            existing_task_mem = self.memory_store.read_task_memory().strip()
            session_name = self.memory_store._current_session_path.stem

            # Phase 1: compact session + existing task memory → new TASK_MEMORY.md
            try:
                compacted_task = await call_planner(
                    system_prompt=(
                        "You are a memory compaction assistant. Merge the new session "
                        "observations into the existing task memory. Remove contradictions, "
                        "deduplicate, and keep only actionable task-specific knowledge "
                        "(map layouts, strategies, item locations, enemy stats, what worked, "
                        "what failed). Output the complete updated task memory as markdown."
                    ),
                    user_prompt=(
                        f"## Existing task memory\n{existing_task_mem or '(empty)'}\n\n"
                        f"## New observations from {session_name}\n{observations}"
                    ),
                )
                if compacted_task and compacted_task.strip():
                    self.memory_store.write_task_memory(compacted_task.strip())
                    print("[TinyClaw] Planner compacted session → TASK_MEMORY.md")
                else:
                    raise ValueError("Planner returned empty compaction")
            except Exception as e:
                print(f"[TinyClaw] Planner compaction failed ({e}), falling back to naive append")
                new_section = f"\n\n## {session_name} observations\n{observations}"
                self.memory_store.write_task_memory(
                    (existing_task_mem + new_section) if existing_task_mem else new_section.strip()
                )
                print("[TinyClaw] Naive append → TASK_MEMORY.md")

            # Phase 2: extract cross-task patterns → MEMORY.md
            updated_task_mem = self.memory_store.read_task_memory().strip()
            existing_memory = self.memory_store.read_file("MEMORY.md").strip()

            try:
                compacted_global = await call_planner(
                    system_prompt=(
                        "You are a memory compaction assistant. Extract cross-task patterns "
                        "from the task-specific memory and merge into global memory. Keep only "
                        "knowledge that generalizes beyond this specific task: general strategies, "
                        "tool usage patterns, environment observations, UI navigation tips. "
                        "Output the complete updated global memory as markdown."
                    ),
                    user_prompt=(
                        f"## Existing global memory\n{existing_memory or '(empty)'}\n\n"
                        f"## Task memory for {task_id}\n{updated_task_mem}"
                    ),
                )
                if compacted_global and compacted_global.strip():
                    self.memory_store.write_memory(compacted_global.strip())
                    print("[TinyClaw] Planner compacted task memory → MEMORY.md")
                else:
                    raise ValueError("Planner returned empty compaction")
            except Exception as e:
                print(f"[TinyClaw] Global compaction failed ({e}), falling back to naive append")
                task_section = f"\n\n## {task_id} / {session_name}\n{observations}"
                self.memory_store.write_memory(
                    (existing_memory + task_section) if existing_memory else task_section.strip()
                )
                print("[TinyClaw] Naive append → MEMORY.md")
        except Exception as e:
            print(f"[TinyClaw] Warning: session consolidation failed: {e}")
