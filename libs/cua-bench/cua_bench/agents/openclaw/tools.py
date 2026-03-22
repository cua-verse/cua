"""Tool registry, summaries, and logging callback for the OpenClaw agent harness.

Centralizes tool assembly (previously inline in openclaw_agent.py) and provides
a ToolLoggingCallback that uses CUA's AsyncCallbackHandler for observability.

Design rationale (US-OC-007):
  - Tool assembly: extracted from inline list to build_tools() for reuse by US-OC-008
  - Tool summaries: extracted to get_tool_summaries() for prompt builder
  - Logging callback: adapted from OpenClaw's wrapToolWithBeforeToolCallHook
    (pi-tools.before-tool-call.ts) — observe-only via CUA callbacks, no blocking/modification

Reference: openclaw/src/agents/pi-tools.ts (createOpenClawCodingTools),
           openclaw/src/agents/pi-tools.before-tool-call.ts (hook wrapping)
"""

from __future__ import annotations

import json
import time
from typing import Any

from agent.callbacks.base import AsyncCallbackHandler
from agent.tools.base import BaseTool

from .memory import MemoryGetTool, MemorySearchTool, MemoryStore, MemoryWriteTool


def build_tools(session, memory_store: MemoryStore, *, summary_model: str | None = None) -> list:
    """Assemble the canonical tool list for the OpenClaw agent.

    Returns [Computer, MilestoneTool, AnalyzeImageTool, MemorySearchTool,
             MemoryGetTool, MemoryWriteTool].

    Args:
        session: CUA DesktopSession (provides ``_computer`` and ``interface``).
        memory_store: Initialized MemoryStore for this task.
        summary_model: Model string for VLM calls in AnalyzeImageTool (defaults to agent's summary_model).
    """
    from agent.tools import AnalyzeImageTool, MilestoneTool

    milestone_tool = MilestoneTool(session.interface)
    analyze_image_tool = AnalyzeImageTool(session.interface, model=summary_model)
    memory_search = MemorySearchTool(memory_store)
    memory_get = MemoryGetTool(memory_store)
    memory_write = MemoryWriteTool(memory_store)

    return [session._computer, milestone_tool, analyze_image_tool, memory_search, memory_get, memory_write]


def get_tool_summaries(tools: list) -> dict[str, str]:
    """Extract ``{name: description}`` from BaseTool instances, filtering out Computer.

    The Computer object is duck-typed (not a BaseTool subclass) and has no
    user-facing description useful for the system prompt.
    """
    return {
        tool.name: tool.description
        for tool in tools
        if isinstance(tool, BaseTool)
    }


# ---------------------------------------------------------------------------
# Logging constants
# ---------------------------------------------------------------------------

_MAX_ARGS_LOG = 200
"""Max characters of serialized arguments to include in start log."""


def _get_action_type_label(item: dict[str, Any]) -> str:
    """Extract a human-readable action type label from a computer_call item.

    Handles both single ``action`` (computer-use-preview) and batched
    ``actions`` array (GPT 5.4).
    """
    # Single action (computer-use-preview)
    action = item.get("action")
    if isinstance(action, dict):
        return action.get("type", "unknown")
    if action is not None:
        return str(action)

    # Batched actions (GPT 5.4)
    actions = item.get("actions")
    if isinstance(actions, list) and actions:
        types = [a.get("type", "?") for a in actions if isinstance(a, dict)]
        return "+".join(types) if types else "unknown"

    return "unknown"

_MAX_RESULT_LOG = 100
"""Max characters of result output to include in end log."""


class ToolLoggingCallback(AsyncCallbackHandler):
    """Logs tool (function) calls with timing via CUA's callback system.

    Adapted from OpenClaw's ``wrapToolWithBeforeToolCallHook`` — but observe-only,
    since CUA callbacks cannot block or modify calls.

    Hooks used:
      - ``on_function_call_start``: log tool name + truncated args, record start time
      - ``on_function_call_end``: log tool name + truncated result + duration
      - ``on_computer_call_start``: log computer action type, record start time
      - ``on_computer_call_end``: log computer action completion + duration
    """

    def __init__(self) -> None:
        self._start_times: dict[str, float] = {}

    # --- Function calls (memory tools, milestone) ---

    async def on_function_call_start(self, item: dict[str, Any]) -> None:
        call_id = item.get("call_id", "unknown")
        name = item.get("name", "unknown")
        args_raw = item.get("arguments", "")

        # Truncate args for log readability
        if isinstance(args_raw, dict):
            args_str = json.dumps(args_raw, ensure_ascii=False)
        else:
            args_str = str(args_raw)
        if len(args_str) > _MAX_ARGS_LOG:
            args_str = args_str[:_MAX_ARGS_LOG] + "…"

        self._start_times[call_id] = time.monotonic()
        print(f"[Tool] {name}({args_str})")

    async def on_function_call_end(
        self, item: dict[str, Any], result: list[dict[str, Any]]
    ) -> None:
        call_id = item.get("call_id", "unknown")
        name = item.get("name", "unknown")

        # Calculate duration
        start = self._start_times.pop(call_id, None)
        duration_ms = round((time.monotonic() - start) * 1000) if start is not None else -1

        # Extract result summary
        result_summary = _extract_result_summary(result)

        duration_str = f"{duration_ms}ms" if duration_ms >= 0 else "?ms"
        print(f"[Tool] {name} -> {result_summary} ({duration_str})")

    # --- Computer calls (mouse, keyboard, screenshot) ---
    # Note: GPT 5.4 uses "actions" (array), computer-use-preview uses "action" (singular)

    async def on_computer_call_start(self, item: dict[str, Any]) -> None:
        call_id = item.get("call_id", "unknown")
        action_type = _get_action_type_label(item)
        self._start_times[call_id] = time.monotonic()
        print(f"[Computer] {action_type}")

    async def on_computer_call_end(
        self, item: dict[str, Any], result: list[dict[str, Any]]
    ) -> None:
        call_id = item.get("call_id", "unknown")
        action_type = _get_action_type_label(item)

        start = self._start_times.pop(call_id, None)
        duration_ms = round((time.monotonic() - start) * 1000) if start is not None else -1
        duration_str = f"{duration_ms}ms" if duration_ms >= 0 else "?ms"
        print(f"[Computer] {action_type} done ({duration_str})")


def _extract_result_summary(result: list[dict[str, Any]]) -> str:
    """Extract a truncated string summary from a function call result list."""
    if not result:
        return "(empty)"

    # Result is typically a list of output dicts; grab the first output string
    for item in result:
        output = item.get("output", "")
        if output:
            s = str(output)
            if len(s) > _MAX_RESULT_LOG:
                return s[:_MAX_RESULT_LOG] + "…"
            return s

    return "(no output)"
