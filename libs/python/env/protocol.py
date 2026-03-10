"""
Core types for CUA desktop environments, extending OpenEnv.

CUA-specific types (DesktopAction, DesktopObservation, DesktopState) extend
OpenEnv's base types (Action, Observation, State) so that CUA environments
are natively compatible with the OpenEnv ecosystem (envbeats assessors,
MCP tool bridges, etc.).

Re-exports OpenEnv's StepResult for convenience.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

from pydantic import Field

# OpenEnv base types
from openenv.core.client_types import StepResult
from openenv.core.env_server.types import (
    Action as OpenEnvAction,
    Observation as OpenEnvObservation,
    State as OpenEnvState,
)


# ---------------------------------------------------------------------------
# CUA-specific types extending OpenEnv
# ---------------------------------------------------------------------------


class DesktopAction(OpenEnvAction):
    """A CUA desktop action extending OpenEnv's Action.

    Examples:
        DesktopAction(type="click", params={"x": 100, "y": 200, "button": "left"})
        DesktopAction(type="type", params={"text": "hello"})
        DesktopAction(type="keypress", params={"keys": ["ctrl", "c"]})
        DesktopAction(type="terminate", params={})
    """

    type: str = Field(description="Action type: click, type, scroll, keypress, etc.")
    params: Dict[str, Any] = Field(
        default_factory=dict, description="Action parameters"
    )

    @staticmethod
    def from_computer_call(item: Dict[str, Any]) -> "DesktopAction":
        """Convert a CUA computer_call item to a DesktopAction.

        CUA format: {"type": "computer_call", "action": {"type": "click", "x": 100, "y": 200}}
        """
        action = item.get("action", {})
        action_type = action.get("type", "")
        params = {k: v for k, v in action.items() if k != "type"}
        return DesktopAction(type=action_type, params=params)

    def to_computer_call(self, call_id: Optional[str] = None) -> Dict[str, Any]:
        """Convert back to CUA computer_call format."""
        return {
            "type": "computer_call",
            "call_id": call_id or str(uuid.uuid4()),
            "action": {"type": self.type, **self.params},
            "pending_safety_checks": [],
            "status": "completed",
        }


class DesktopObservation(OpenEnvObservation):
    """CUA desktop observation extending OpenEnv's Observation.

    Inherits from OpenEnv: done (bool), reward (float|None), metadata (dict).
    Adds: screenshot (base64 PNG string).
    """

    screenshot: Optional[str] = Field(
        default=None, description="Base64-encoded PNG screenshot"
    )


class DesktopState(OpenEnvState):
    """CUA desktop environment state extending OpenEnv's State.

    Inherits from OpenEnv: episode_id (str|None), step_count (int).
    Adds: dimensions, environment_type.
    """

    dimensions: Optional[tuple[int, int]] = Field(
        default=None, description="Screen dimensions (width, height)"
    )
    environment_type: Optional[str] = Field(
        default=None, description="OS type: windows, mac, linux, browser"
    )


# ---------------------------------------------------------------------------
# Local helper types (no OpenEnv equivalent)
# ---------------------------------------------------------------------------


@dataclass
class TaskSpec:
    """A task to be completed in the environment."""

    instruction: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ActionSpec:
    """Describes what actions the environment supports."""

    supported_actions: List[str]


@dataclass
class ObservationSpec:
    """Describes what observations the environment provides."""

    has_screenshot: bool = True
    screen_dimensions: Optional[tuple[int, int]] = None
    environment_type: Optional[Literal["windows", "mac", "linux", "browser"]] = None
