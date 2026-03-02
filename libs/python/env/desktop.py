"""Desktop environment wrapping an existing AsyncComputerHandler.

Extends OpenEnv's Environment base class so that CUA desktop environments
are natively compatible with the OpenEnv ecosystem. Envbeats assessors
can serve this as an OpenEnv environment, and agents can interact with
it through OpenEnv's standard reset/step/state interface.

Also provides CUA-specific convenience methods (observe, action_spec,
obs_spec) used by CUALoopAgent and MCPToolBridge.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from openenv.core.env_server.interfaces import Environment

from .protocol import (
    ActionSpec,
    DesktopAction,
    DesktopObservation,
    DesktopState,
    ObservationSpec,
    TaskSpec,
)

# Import type only — the actual handler instance is passed in at runtime.
from agent.computers.base import AsyncComputerHandler


class DesktopEnv(Environment[DesktopAction, DesktopObservation, DesktopState]):
    """Wraps an AsyncComputerHandler into an OpenEnv Environment.

    Accepts any object implementing the AsyncComputerHandler protocol
    (cuaComputerHandler, CustomComputerHandler, or user-provided).
    All existing handler infrastructure is reused without modification.

    OpenEnv interface (for envbeats / external tools):
        - reset() / reset_async() -> DesktopObservation
        - step() / step_async() -> DesktopObservation
        - state (property) -> DesktopState

    CUA convenience methods (for CUALoopAgent / MCPToolBridge):
        - observe() -> DesktopObservation
        - action_spec() -> ActionSpec
        - obs_spec() -> ObservationSpec
    """

    SUPPORTS_CONCURRENT_SESSIONS = False

    SUPPORTED_ACTIONS = [
        "click",
        "double_click",
        "scroll",
        "type",
        "wait",
        "move",
        "keypress",
        "drag",
        "screenshot",
        "terminate",
        "left_mouse_down",
        "left_mouse_up",
        "get_current_url",
    ]

    def __init__(
        self,
        computer_handler: AsyncComputerHandler,
        screenshot_delay: float = 0.0,
    ):
        super().__init__()  # Environment.__init__ (no transform)
        self._handler = computer_handler
        self._screenshot_delay = screenshot_delay
        self._state = DesktopState()

    # ------------------------------------------------------------------
    # OpenEnv Environment interface
    # ------------------------------------------------------------------

    def reset(
        self,
        seed: Optional[int] = None,
        episode_id: Optional[str] = None,
        **kwargs: Any,
    ) -> DesktopObservation:
        """Sync reset — delegates to async version via asyncio.run().

        Prefer reset_async() when already in an async context.
        """
        return asyncio.run(
            self.reset_async(seed=seed, episode_id=episode_id, **kwargs)
        )

    async def reset_async(
        self,
        seed: Optional[int] = None,
        episode_id: Optional[str] = None,
        **kwargs: Any,
    ) -> DesktopObservation:
        """Initialize environment. Returns initial observation with screenshot."""
        # Cache env info
        try:
            dims = await self._handler.get_dimensions()
        except Exception:
            dims = (1024, 768)
        try:
            env_type = await self._handler.get_environment()
        except Exception:
            env_type = "linux"

        self._state = DesktopState(
            episode_id=episode_id,
            step_count=0,
            dimensions=dims,
            environment_type=env_type,
        )

        screenshot = await self._handler.screenshot()
        return DesktopObservation(screenshot=screenshot)

    def step(
        self,
        action: DesktopAction,
        timeout_s: Optional[float] = None,
        **kwargs: Any,
    ) -> DesktopObservation:
        """Sync step — delegates to async version via asyncio.run().

        Prefer step_async() when already in an async context.
        """
        return asyncio.run(
            self.step_async(action, timeout_s=timeout_s, **kwargs)
        )

    async def step_async(
        self,
        action: DesktopAction,
        timeout_s: Optional[float] = None,
        **kwargs: Any,
    ) -> DesktopObservation:
        """Execute action on computer handler, return observation with screenshot."""
        # Handle screenshot-only action
        if action.type == "screenshot":
            screenshot = await self._handler.screenshot()
            return DesktopObservation(screenshot=screenshot)

        # Handle terminate action
        if action.type == "terminate":
            return DesktopObservation(done=True)

        # Dispatch action to handler method
        method = getattr(self._handler, action.type, None)
        if method is None:
            raise ValueError(
                f"Unsupported action type: {action.type}. "
                f"Supported: {self.SUPPORTED_ACTIONS}"
            )

        result = await method(**action.params)

        # Check if the handler itself signals termination
        if isinstance(result, dict) and result.get("terminated"):
            return DesktopObservation(done=True)

        # Take screenshot after action
        if self._screenshot_delay > 0:
            await asyncio.sleep(self._screenshot_delay)
        screenshot = await self._handler.screenshot()

        self._state.step_count += 1
        return DesktopObservation(screenshot=screenshot)

    @property
    def state(self) -> DesktopState:
        """Current environment state."""
        return self._state

    def close(self) -> None:
        """No-op — handler lifecycle is managed externally."""
        pass

    # ------------------------------------------------------------------
    # CUA convenience methods (not part of OpenEnv interface)
    # ------------------------------------------------------------------

    async def observe(self) -> DesktopObservation:
        """Get current observation without taking an action."""
        screenshot = await self._handler.screenshot()
        return DesktopObservation(screenshot=screenshot)

    def action_spec(self) -> ActionSpec:
        """What actions this environment supports."""
        return ActionSpec(supported_actions=self.SUPPORTED_ACTIONS)

    def obs_spec(self) -> ObservationSpec:
        """What observations this environment provides."""
        return ObservationSpec(
            has_screenshot=True,
            screen_dimensions=self._state.dimensions,
            environment_type=self._state.environment_type,
        )
