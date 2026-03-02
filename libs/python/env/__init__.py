"""
cua-env - Environment protocol for decoupled agent-environment interaction.

Built on top of OpenEnv (openenv-core). CUA-specific types extend OpenEnv's
base types so that CUA desktop environments are natively compatible with
the OpenEnv ecosystem.

Key types:
  - DesktopAction, DesktopObservation, DesktopState (extend OpenEnv)
  - DesktopEnv (extends openenv.Environment, wraps AsyncComputerHandler)
  - CUALoopAgent (wraps existing CUA agent loops into AgentProtocol)
  - StepResult (re-exported from OpenEnv)
"""

from openenv.core.client_types import StepResult

from .protocol import (
    ActionSpec,
    DesktopAction,
    DesktopObservation,
    DesktopState,
    ObservationSpec,
    TaskSpec,
)
from .desktop import DesktopEnv
from .agent_protocol import AgentProtocol, AgentResult, CUALoopAgent, EnvBackedHandler
from .shortcuts import make_agent_from_model, make_env_from_computer

__all__ = [
    # OpenEnv re-exports
    "StepResult",
    # CUA types (extending OpenEnv)
    "DesktopAction",
    "DesktopObservation",
    "DesktopState",
    # Local helper types
    "TaskSpec",
    "ActionSpec",
    "ObservationSpec",
    # Environment implementation
    "DesktopEnv",
    # Agent protocol
    "AgentProtocol",
    "AgentResult",
    "CUALoopAgent",
    "EnvBackedHandler",
    # Convenience
    "make_env_from_computer",
    "make_agent_from_model",
]
