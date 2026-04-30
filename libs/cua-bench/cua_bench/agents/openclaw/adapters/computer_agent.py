"""``OpenClawImageAwareComputerAgent`` — historical alias.

Kept as a thin subclass of ``OpenClawComputerAgent`` for back-compat with
call sites (e.g. orchestration's ``agents/openclaw_cua/agent.py``) that
imported it when the dispatch logic lived here. All behavior — B2
``function_call name="computer"`` dispatch, B3 image-shaped tool returns,
``auto_screenshot`` gating — now lives on the parent ``OpenClawComputerAgent``
in ``../agent_loop.py`` so the SDK ``ComputerAgent`` stays diff-free
against ``cua-verse/cua`` upstream.

New code should import ``OpenClawComputerAgent`` directly. This shim
exists only so existing imports keep working.
"""

from __future__ import annotations

from cua_bench.agents.openclaw.agent_loop import OpenClawComputerAgent


class OpenClawImageAwareComputerAgent(OpenClawComputerAgent):
    """Back-compat alias — see module docstring."""
