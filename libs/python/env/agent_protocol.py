"""Agent protocol and wrapper for existing CUA agent loops.

Defines AgentProtocol (the black-box agent interface) and CUALoopAgent
(wrapper that adapts existing AsyncAgentConfig loops to AgentProtocol).
Also provides EnvBackedHandler for loops that access computer_handler
during predict_step().

Uses OpenEnv's StepResult for step results returned from the environment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, TYPE_CHECKING

from openenv.core.client_types import StepResult

from .protocol import DesktopAction, DesktopObservation, TaskSpec

if TYPE_CHECKING:
    from .desktop import DesktopEnv


@dataclass
class AgentResult:
    """Result returned by an agent after completing (or failing) a task."""

    success: bool
    message: str = ""
    trajectory: List[Dict[str, Any]] = field(default_factory=list)


class AgentProtocol(Protocol):
    """Black-box agent interface for evaluation.

    The agent receives a task and an environment handle.
    It drives its own loop, calling env.step_async() as needed.
    Returns when done.
    """

    async def run(self, task: TaskSpec, env: "DesktopEnv") -> AgentResult:
        """Execute the task using the environment. Agent owns the loop."""
        ...


class EnvBackedHandler:
    """Minimal AsyncComputerHandler shim backed by DesktopEnv.

    Only implements the read-only methods that agent loops call internally
    during predict_step(): screenshot(), get_dimensions(), get_environment().
    All action methods raise NotImplementedError — actions should go through
    env.step_async() in the outer loop, not through the handler.
    """

    def __init__(self, env: "DesktopEnv"):
        self._env = env

    async def screenshot(self, text: Optional[str] = None) -> str:
        obs = await self._env.observe()
        return obs.screenshot or ""

    async def get_dimensions(self) -> tuple[int, int]:
        return self._env.state.dimensions or (1024, 768)

    async def get_environment(self):
        return self._env.state.environment_type or "linux"

    # --- Action methods: not supported via handler ---
    # These exist so the handler passes isinstance checks but should
    # never be called — actions go through env.step_async() in the outer loop.

    async def click(self, *args, **kwargs):
        raise NotImplementedError("Use env.step_async() for actions, not handler")

    async def double_click(self, *args, **kwargs):
        raise NotImplementedError("Use env.step_async() for actions, not handler")

    async def scroll(self, *args, **kwargs):
        raise NotImplementedError("Use env.step_async() for actions, not handler")

    async def type(self, *args, **kwargs):
        raise NotImplementedError("Use env.step_async() for actions, not handler")

    async def wait(self, *args, **kwargs):
        raise NotImplementedError("Use env.step_async() for actions, not handler")

    async def move(self, *args, **kwargs):
        raise NotImplementedError("Use env.step_async() for actions, not handler")

    async def keypress(self, *args, **kwargs):
        raise NotImplementedError("Use env.step_async() for actions, not handler")

    async def drag(self, *args, **kwargs):
        raise NotImplementedError("Use env.step_async() for actions, not handler")

    async def get_current_url(self) -> str:
        raise NotImplementedError("Use env.step_async() for actions, not handler")

    async def left_mouse_down(self, *args, **kwargs):
        raise NotImplementedError("Use env.step_async() for actions, not handler")

    async def left_mouse_up(self, *args, **kwargs):
        raise NotImplementedError("Use env.step_async() for actions, not handler")


class CUALoopAgent:
    """Wraps an existing CUA AsyncAgentConfig loop into AgentProtocol.

    This allows any of the 20+ existing agent loops to be used with
    the OpenEnv-based DesktopEnv without modifying them. The wrapper:
    1. Calls env.reset_async() to get initial observation
    2. Builds messages in CUA format
    3. Calls loop.predict_step() to get actions
    4. Converts actions to env.step_async() calls
    5. Loops until done or assistant message
    """

    def __init__(
        self,
        agent_loop,  # AsyncAgentConfig instance
        model: str,
        tools: Optional[List[Dict]] = None,
        max_steps: int = 100,
        **generation_config,
    ):
        self.loop = agent_loop
        self.model = model
        self.tools = tools
        self.max_steps = max_steps
        self.generation_config = generation_config

    async def run(self, task: TaskSpec, env: "DesktopEnv") -> AgentResult:
        """Run the CUA loop against the DesktopEnv."""
        trajectory: List[Dict[str, Any]] = []

        # Initial observation via OpenEnv reset
        obs = await env.reset_async(episode_id=None)
        messages = self._build_initial_messages(task, obs)

        # Create EnvBackedHandler for loops that read from handler in predict_step
        env_handler = EnvBackedHandler(env)

        for step_idx in range(self.max_steps):
            # Call the existing agent loop
            result = await self.loop.predict_step(
                messages=messages,
                model=self.model,
                tools=self.tools,
                computer_handler=env_handler,
                **self.generation_config,
            )

            output_items = result.get("output", [])
            handled_action = False

            for item in output_items:
                item_type = item.get("type")

                if item_type == "computer_call":
                    # Convert to DesktopAction, execute via env
                    action = DesktopAction.from_computer_call(item)
                    obs = await env.step_async(action)

                    trajectory.append(
                        {
                            "step": step_idx,
                            "action": {"type": action.type, **action.params},
                            "done": obs.done,
                            "reward": obs.reward,
                        }
                    )

                    if obs.done:
                        return AgentResult(
                            success=True,
                            message="Task completed (terminate action)",
                            trajectory=trajectory,
                        )

                    # Append the agent's full output + observation to messages
                    messages += output_items
                    messages.append(self._make_call_output(item, obs))
                    handled_action = True
                    break  # next predict_step iteration

                elif item_type == "message":
                    # Agent decided to stop with a text message
                    content = self._extract_text(item)
                    return AgentResult(
                        success=True,
                        message=content,
                        trajectory=trajectory,
                    )

            if not handled_action:
                # No actionable items — agent may have returned reasoning only
                messages += output_items

        return AgentResult(
            success=False,
            message=f"Max steps ({self.max_steps}) reached",
            trajectory=trajectory,
        )

    # --- Helper methods ---

    def _build_initial_messages(
        self, task: TaskSpec, obs: DesktopObservation
    ) -> List[Dict[str, Any]]:
        """Build initial CUA-format messages from task + first observation."""
        messages: List[Dict[str, Any]] = [
            {"role": "user", "content": task.instruction}
        ]
        if obs.screenshot:
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_image",
                            "image_url": f"data:image/png;base64,{obs.screenshot}",
                        }
                    ],
                }
            )
        return messages

    def _make_call_output(
        self, item: Dict[str, Any], obs: DesktopObservation
    ) -> Dict[str, Any]:
        """Build a computer_call_output message from an observation."""
        output: Dict[str, Any] = {
            "type": "computer_call_output",
            "call_id": item.get("call_id"),
            "acknowledged_safety_checks": [],
        }
        if obs.screenshot:
            output["output"] = {
                "type": "input_image",
                "image_url": f"data:image/png;base64,{obs.screenshot}",
            }
        else:
            output["output"] = {}
        return output

    def _extract_text(self, item: Dict[str, Any]) -> str:
        """Extract text content from a message item."""
        content = item.get("content", [])
        if isinstance(content, str):
            return content
        texts = []
        for c in content:
            if isinstance(c, dict) and c.get("text"):
                texts.append(c["text"])
        return "\n".join(texts)
