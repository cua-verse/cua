"""Native CUA Agent — passes the session's Computer directly to ComputerAgent.

Unlike CuaAgent which builds a custom computer dict (with hardcoded dimensions
and environment), this agent extracts the Computer instance from the
RemoteDesktopSession and hands it to ComputerAgent as a tool. This means
ComputerAgent uses cuaComputerHandler which reads the real screen size from
the VM and maps coordinates correctly.
"""

import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from . import register_agent
from .base import AgentResult, BaseAgent, FailureMode

if TYPE_CHECKING:
    from ..computers import DesktopSession


@register_agent("native-cua-agent")
class NativeCuaAgent(BaseAgent):
    """Agent that passes the session's Computer instance directly to ComputerAgent."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.model = kwargs.get("model", "anthropic/claude-sonnet-4-20250514")
        self.max_steps = kwargs.get("max_steps", 100)
        self.task_retries = int(kwargs.get("task_retries", 0))

    @staticmethod
    def name() -> str:
        return "native-cua-agent"

    async def perform_task(
        self,
        task_description: str,
        session: "DesktopSession",
        logging_dir: Path | None = None,
        tracer=None,
    ) -> AgentResult:
        try:
            from agent import ComputerAgent
        except ImportError as e:
            raise RuntimeError(
                "native-cua-agent requires the `cua-agent` package. "
                "Install with: pip install cua-agent"
            ) from e

        # Extract the Computer instance from the session
        computer = getattr(session, "_computer", None) or getattr(session, "computer", None)
        if computer is None:
            raise RuntimeError(
                "Session has no Computer instance. "
                "Ensure the session is started before calling perform_task."
            )

        # Render instruction with template if provided
        instruction = self._render_instruction(task_description)

        # Trajectory directory
        trajectory_dir = None
        if logging_dir:
            trajectory_dir = logging_dir / "trajectories"
            trajectory_dir.mkdir(parents=True, exist_ok=True)

        # Create agent with the real Computer object as tool.
        # ComputerAgent will use cuaComputerHandler which calls
        # interface.get_screen_size() for dimensions.
        agent = ComputerAgent(
            model=self.model,
            tools=[computer],
            only_n_most_recent_images=3,
            trajectory_dir=trajectory_dir,
            instructions=(
                "Use the provided computer to complete the task as described. "
                "When the task is complete, indicate so clearly by outputting 'DONE'."
            ),
        )
        print(f"NativeCuaAgent initialized with model: {self.model}")

        # Run agent with task-level retry on transient API errors
        total_usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "response_cost": 0.0,
        }
        last_exc: BaseException | None = None

        for attempt in range(self.task_retries + 1):
            if attempt > 0:
                delay = 5.0 * (2 ** (attempt - 1))
                print(
                    f"[native-cua-agent] Retrying (attempt {attempt + 1}/{self.task_retries + 1}) "
                    f"after: {last_exc!r}. Waiting {delay:.0f}s..."
                )
                await asyncio.sleep(delay)

            try:
                step = 0
                task_completed = False

                async for result in agent.run(instruction):
                    sys.stdout.flush()
                    step += 1

                    for k in total_usage:
                        total_usage[k] += result["usage"].get(k, 0)

                    # Record to tracer
                    if tracer:
                        try:
                            screenshot = await session.screenshot()
                            usage = result["usage"]
                            if hasattr(usage, "model_dump"):
                                usage = usage.model_dump()
                            elif not isinstance(usage, dict):
                                usage = dict(usage)
                            tracer.record(
                                "agent_step",
                                {
                                    "step": step,
                                    "agent": self.name(),
                                    "model": self.model,
                                    "usage": usage,
                                    "output": result["output"],
                                },
                                [screenshot],
                            )
                        except Exception as e:
                            print(f"Warning: Failed to record agent step: {e}")

                    # Check max steps
                    if step >= self.max_steps:
                        print(f"\n[Max steps reached] Stopped at step {step}/{self.max_steps}")
                        break

                    # Check if agent signalled completion
                    for item in result["output"]:
                        if item.get("type") == "message":
                            for content in item.get("content", []):
                                if isinstance(content, dict) and "DONE" in content.get("text", ""):
                                    print(f"\n[Task completed] Agent indicated completion at step {step}")
                                    task_completed = True
                                    break
                        if task_completed:
                            break
                    if task_completed:
                        break

                # Determine failure mode
                failure_mode = FailureMode.NONE
                if not task_completed and step >= self.max_steps:
                    failure_mode = FailureMode.MAX_STEPS_EXCEEDED

                print(f"\nTotal usage: {total_usage}")
                print(f"Steps completed: {step}/{self.max_steps}")

                return AgentResult(
                    total_input_tokens=total_usage.get("prompt_tokens", 0),
                    total_output_tokens=total_usage.get("completion_tokens", 0),
                    failure_mode=failure_mode,
                )

            except Exception as e:
                last_exc = e
                if attempt < self.task_retries:
                    # Check if retryable
                    msg = str(e).lower()
                    if any(k in msg for k in ("timeout", "rate limit", "503", "502", "429", "connection")):
                        continue
                # Not retryable or out of retries
                raise

        # Should not reach here, but just in case
        raise RuntimeError(f"All {self.task_retries + 1} attempts failed. Last error: {last_exc}")
