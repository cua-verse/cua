"""Convenience constructors for common agent+env setups."""

from __future__ import annotations

from .desktop import DesktopEnv
from .agent_protocol import CUALoopAgent


async def make_env_from_computer(
    computer, screenshot_delay: float = 0.0
) -> DesktopEnv:
    """Create a DesktopEnv from any CUA-compatible computer object.

    Accepts: Computer instance, AsyncComputerHandler, or dict of functions.
    Uses the existing make_computer_handler() factory internally.

    The returned DesktopEnv is a full OpenEnv Environment and can be
    served by the OpenEnv server framework or used in-process.
    """
    from agent.computers import make_computer_handler

    handler = await make_computer_handler(computer)
    return DesktopEnv(handler, screenshot_delay=screenshot_delay)


def make_agent_from_model(model: str, **kwargs) -> CUALoopAgent:
    """Create a CUALoopAgent by auto-selecting the loop for a model name.

    Uses the existing @register_agent registry to find the right loop
    for the given model string (e.g. "anthropic/claude-sonnet-4-20250514").
    """
    from agent.decorators import find_agent_config

    config_info = find_agent_config(model)
    if not config_info:
        raise ValueError(f"No agent loop registered for model: {model}")
    loop = config_info.agent_class()
    return CUALoopAgent(agent_loop=loop, model=model, **kwargs)
