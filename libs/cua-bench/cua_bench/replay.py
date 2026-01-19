"""Trajectory replay utilities for cua-bench.

Provides functions to replay recorded agent trajectories by executing
computer_call actions from agent_response.json files.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .computers.base import DesktopSession

logger = logging.getLogger(__name__)


async def replay_trajectory(
    trajectory_dir: str | Path,
    session: "DesktopSession",
    action_delay: float = 0.5,
) -> int:
    """
    Replay a trajectory by executing all computer_call actions in sequence.

    Finds the agent_response.json with the highest index (most complete history),
    extracts all computer_call actions from kwargs.messages, and executes them.

    Uses cuaComputerHandler from cua-agent SDK, same as ComputerAgent._handle_item().

    Args:
        trajectory_dir: Path to the trajectory folder containing agent_response.json files
        session: The desktop session to execute actions on
        action_delay: Delay between actions in seconds

    Returns:
        Number of actions executed

    Example:
        ```python
        from cua_bench import make
        from cua_bench.replay import replay_trajectory

        env = make("./my_task")
        await env.reset()
        actions = await replay_trajectory(
            trajectory_dir="./demonstrations/my_trajectory",
            session=env.session,
            action_delay=0.5
        )
        ```
    """
    try:
        from agent.computers import cuaComputerHandler
    except ImportError as e:
        raise ImportError(
            "replay_trajectory requires the 'cua-agent' package. "
            "Install it with: pip install cua-agent"
        ) from e
    # breakpoint()
    logger.info(f"Replaying trajectory from: {trajectory_dir}")
    if not trajectory_dir:
        raise ValueError("trajectory_dir is None or empty")

    trajectory_path = Path(trajectory_dir)
    if not trajectory_path.exists():
        raise FileNotFoundError(f"Trajectory directory not found: {trajectory_dir}")

    # Find all agent_response.json files and sort by filename to get the latest one
    response_files = sorted(trajectory_path.rglob("*_agent_response.json"))
    if not response_files:
        logger.warning(f"No agent_response.json files found in {trajectory_dir}")
        return 0

    # Get the file with the highest index (most complete conversation history)
    latest_response_file = response_files[-1]
    logger.info(
        f"Found {len(response_files)} agent_response files, using latest: {latest_response_file.name}"
    )

    # Load the latest response file
    with open(latest_response_file, "r") as f:
        data = json.load(f)

    # Extract all computer_call actions from kwargs.messages (cumulative history)
    messages = data.get("kwargs", {}).get("messages", [])

    # Collect all computer_call actions in order
    actions_to_execute = []
    for item in messages:
        if isinstance(item, dict) and item.get("type") == "computer_call":
            action = item.get("action", {})
            action_type = action.get("type")
            if action_type and action_type != "screenshot":
                actions_to_execute.append(action)

    logger.info(f"Found {len(actions_to_execute)} actions to replay from conversation history")

    if not actions_to_execute:
        logger.warning("No actions found to replay")
        return 0

    # Create computer handler from session._computer (same as ComputerAgent does)
    handler = cuaComputerHandler(session._computer)
    await handler._initialize()

    actions_executed = 0

    for i, action in enumerate(actions_to_execute):
        action_type = action.get("type")
        # Extract action arguments (all fields except 'type')
        action_args = {k: v for k, v in action.items() if k != "type"}

        logger.info(f"[{i+1}/{len(actions_to_execute)}] Executing: {action_type}({action_args})")

        # Dynamic dispatch - same pattern as ComputerAgent._handle_item()
        method = getattr(handler, action_type, None)
        if method:
            await method(**action_args)
            actions_executed += 1
        else:
            logger.warning(f"Unknown action type: {action_type}")

        # Delay between actions for stability
        await asyncio.sleep(action_delay)

    logger.info(f"Trajectory replay complete. Executed {actions_executed} actions.")
    return actions_executed
