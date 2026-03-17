"""OpenClaw Agent — faithful reproduction of OpenClaw's agent-side architecture for CUA.

Adapts OpenClaw's context management (system prompt construction, compaction pipeline,
memory recall, tool loop, session persistence) to CUA's constraints:
  - instructions= for persistent context (only content never truncated)
  - Trajectory-based observation (CUA reasoning in trajectory files, not conversation)

US-OC-017: Uses OpenClawComputerAgent subclass for mid-loop compaction instead of
the stop-compact-resume pattern. Compaction happens in-place inside run() — no
agent rebuild needed.

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


if TYPE_CHECKING:
    from ..computers import DesktopSession


@register_agent("openclaw-agent")
class OpenClawAgent(BaseAgent):
    """OpenClaw agent reproduction for CUA benchmark framework."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.model = kwargs.get("model", "anthropic/claude-sonnet-4-20250514")
        # Separate model for summarization and memory flush (defaults to main model)
        self.summary_model = kwargs.get("summary_model", None) or self.model
        self.max_steps = kwargs.get("max_steps", 100)
        self.max_history_turns = kwargs.get("max_history_turns", None)  # None = all

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
        Perform a task using the OpenClawComputerAgent with mid-loop compaction.

        Uses OpenClawComputerAgent (US-OC-017) which handles compaction in-place
        inside run() — no stop-compact-resume pattern needed.

        Args:
            task_description: The task description/instruction
            session: The desktop session to interact with
            logging_dir: Optional directory for logging agent execution
            tracer: Optional tracer object for recording agent actions

        Returns:
            AgentResult with token counts and failure mode
        """
        try:
            from agent import ComputerAgent  # noqa: F401 — validate package is installed
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

        # Build structured system prompt via PromptBuilder (US-OC-001)
        from .openclaw import (
            ContextFile,
            ContextOverflowCallback,
            MemoryStore,
            OpenClawComputerAgent,
            PromptBuilder,
            SessionManager,
            ToolLoggingCallback,
            build_replay_messages,
            build_system_prompt_report,
            build_tools,
            convert_to_responses_api_items,
            get_tool_summaries,
            limit_history_turns,
            sanitize_history,
        )

        # Initialize memory store (US-OC-002)
        # Derive task_id from logging_dir name or fall back to "default"
        task_id = logging_dir.parent.name if logging_dir else "default"
        memory_store = MemoryStore(task_id=task_id)
        memory_store.init_session()

        # Initialize session persistence (US-OC-004)
        session_mgr = SessionManager(task_id=task_id)
        session_mgr.init_session(model=self.model)

        # Cross-run continuity (US-OC-012): replay prior transcript as messages
        # so the agent sees actual conversation history from previous runs.
        prior_entries = session_mgr.load_history()
        replay_messages: list[dict[str, Any]] = []
        if prior_entries:
            replay_messages = build_replay_messages(prior_entries)
            replay_messages = sanitize_history(replay_messages)
            replay_messages = limit_history_turns(replay_messages, self.max_history_turns)
            # Re-sanitize after truncation (may orphan tool results at cut point)
            replay_messages = sanitize_history(replay_messages)
            # Unnest Chat Completions messages into Responses API items (US-OC-022)
            replay_messages = convert_to_responses_api_items(replay_messages)
            if replay_messages:
                print(f"[Replay] Loaded {len(replay_messages)} items from prior transcript")

        # Tool assembly (US-OC-007)
        tools = build_tools(session, memory_store)
        tool_summaries = get_tool_summaries(tools)
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

        # System prompt report for observability (US-OC-008)
        report = build_system_prompt_report(
            system_prompt=instructions,
            context_files=context_files,
            tool_summaries=tool_summaries,
            tools=tools,
        )
        session_mgr.set_system_prompt_report(report)

        # Context overflow detection (US-OC-005)
        # Allow env override for testing (e.g. CONTEXT_WINDOW_OVERRIDE=50000)
        import os
        ctx_override = os.environ.get("CONTEXT_WINDOW_OVERRIDE")
        overflow_cb = ContextOverflowCallback(
            model=self.model,
            context_window=int(ctx_override) if ctx_override else None,
            instructions_tokens=len(instructions) // 4,
        )

        # Persist resolved context window in session state (matches OpenClaw's contextTokens)
        if session_mgr._state is not None:
            session_mgr._state.contextTokens = overflow_cb.context_window
            session_mgr.save_state()

        # Create OpenClawComputerAgent with mid-loop compaction support (US-OC-017)
        # overflow_cb is auto-injected into callbacks by OpenClawComputerAgent (US-OC-028)
        tool_logging_cb = ToolLoggingCallback()
        agent = OpenClawComputerAgent(
            # ComputerAgent params
            model=self.model,
            tools=tools,
            only_n_most_recent_images=3,
            trajectory_dir=trajectory_dir,
            instructions=instructions,
            callbacks=[tool_logging_cb],
            # OpenClaw compaction params
            overflow_cb=overflow_cb,
            session_mgr=session_mgr,
            memory_store=memory_store,
            summary_model=self.summary_model,
        )
        print("OpenClaw Agent initialized with model:", self.model)
        if self.summary_model != self.model:
            print("  Summary/flush model:", self.summary_model)

        # Single-loop execution (US-OC-017)
        # Compaction happens in-place inside OpenClawComputerAgent.run() — no
        # stop-compact-resume pattern needed. Reactive overflow is also handled
        # inside the custom run() via try/except around predict_step().
        try:
            total_usage = {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "response_cost": 0.0,
            }

            step = 0
            step_offset = session_mgr.get_step_count()
            task_completed = False

            # Pass replay messages + instruction as the messages arg (US-OC-012).
            # When no prior history, replay_messages is empty and this is
            # equivalent to agent.run(instruction).
            run_input = (
                replay_messages + [{"role": "user", "content": instruction}]
                if replay_messages
                else instruction
            )

            async for result in agent.run(run_input):
                sys.stdout.flush()
                step += 1
                for k in total_usage:
                    total_usage[k] += result["usage"].get(k, 0)

                # Session persistence tracking (US-OC-004)
                step_input = result["usage"].get("input_tokens", 0)
                step_output = result["usage"].get("output_tokens", 0)
                session_mgr.update_step_count(step_offset + step)
                session_mgr.update_tokens(step_input, step_output)

                # Tracer recording (optional)
                if tracer:
                    await _record_tracer_step(tracer, session, step, self.model, result)

                if step >= self.max_steps:
                    print(f"\n[Max steps reached] Stopped at step {step}/{self.max_steps}")
                    break

                task_completed = _check_done(result)
                if task_completed:
                    print(f"\n[Task completed] Agent indicated completion at step {step}")
                    break

            print(f"\nTotal usage: {total_usage}")
            print(f"Steps completed: {step}/{self.max_steps}")
            if agent.compaction_count > 0:
                print(f"Compactions performed: {agent.compaction_count}")

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
                total_input_tokens=total_usage.get("input_tokens", 0),
                total_output_tokens=total_usage.get("output_tokens", 0),
                failure_mode=FailureMode.UNKNOWN,
            )



async def _record_tracer_step(tracer, session, step: int, model: str, result: dict) -> None:
    """Record an agent step to the tracer (optional observability)."""
    try:
        screenshot = await session.screenshot()
        tracer.record(
            "agent_step",
            {
                "step": step,
                "agent": "openclaw-agent",
                "model": model,
                "usage": result["usage"],
                "output": result["output"],
            },
            [screenshot],
        )
    except Exception as e:
        print(f"Warning: Failed to record agent step to tracer: {e}")


def _check_done(result: dict) -> bool:
    """Check if the agent indicated task completion (DONE signal)."""
    for item in result["output"]:
        if item["type"] == "message":
            if "DONE" in item["content"][0]["text"]:
                return True
    return False
