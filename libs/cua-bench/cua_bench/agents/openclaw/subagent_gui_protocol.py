"""GUI subagent — vision-to-action relay protocol (types, parsing, execution).

Contract layer between the GUI subagent (vision model) and the main agent
(action executor). No LLM calls, no VM interaction — just typed actions,
multi-format parsing, and dispatch to RemoteDesktopSession methods.

Primary parse path is OpenAI's native `computer_call` format emitted by GPT-5.4
(the primary GUI subagent model). A function-calling fallback covers non-CU
vision models, and a structured-text path is a last resort.

The relay loop itself (screenshot → model call → parse → execute → repeat)
lives in US-SUB-004; this module provides the pieces that loop composes.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Union


# ---------------------------------------------------------------------------
# GUIAction types
# ---------------------------------------------------------------------------


@dataclass
class GUIClickAction:
    x: int
    y: int
    button: str = "left"  # "left" | "right" | "double"


@dataclass
class GUITypeAction:
    text: str


@dataclass
class GUIHotkeyAction:
    keys: list[str] = field(default_factory=list)


@dataclass
class GUIScrollAction:
    x: int
    y: int
    direction: str = "down"  # "up" | "down"
    amount: int = 3


@dataclass
class GUIDragAction:
    start_x: int
    start_y: int
    end_x: int
    end_y: int


@dataclass
class GUIWaitAction:
    ms: int = 1000


@dataclass
class GUIDoneAction:
    summary: str = ""


GUIAction = Union[
    GUIClickAction,
    GUITypeAction,
    GUIHotkeyAction,
    GUIScrollAction,
    GUIDragAction,
    GUIWaitAction,
    GUIDoneAction,
]


_VALID_BUTTONS = frozenset({"left", "right", "double"})
_VALID_DIRECTIONS = frozenset({"up", "down"})

# Normalize key names from model output to CUA SDK names.
# Matches the mapping in agent/computers/cua.py:98-101.
_KEY_ALIASES: dict[str, str] = {
    "arrowup": "up",
    "arrowdown": "down",
    "arrowleft": "left",
    "arrowright": "right",
}


def _normalize_key(key: str) -> str:
    return _KEY_ALIASES.get(key.lower(), key)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_gui_action(action: GUIAction) -> None:
    """Validate a GUIAction; raise ValueError if malformed.

    Rules:
    - coordinates (x, y, start_*, end_*) must be non-negative
    - GUITypeAction.text must be non-empty
    - GUIHotkeyAction.keys must be non-empty
    - GUIScrollAction.amount must be positive
    - GUIScrollAction.direction must be "up" or "down"
    - GUIClickAction.button must be left/right/double
    - GUIWaitAction.ms must be positive
    """
    if isinstance(action, GUIClickAction):
        if action.x < 0 or action.y < 0:
            raise ValueError(f"Click coordinates must be non-negative: ({action.x}, {action.y})")
        if action.button not in _VALID_BUTTONS:
            raise ValueError(f"Invalid click button: {action.button!r}")
    elif isinstance(action, GUITypeAction):
        if not action.text:
            raise ValueError("Type action text must be non-empty")
    elif isinstance(action, GUIHotkeyAction):
        if not action.keys:
            raise ValueError("Hotkey action keys must be non-empty")
    elif isinstance(action, GUIScrollAction):
        if action.x < 0 or action.y < 0:
            raise ValueError(f"Scroll coordinates must be non-negative: ({action.x}, {action.y})")
        if action.amount <= 0:
            raise ValueError(f"Scroll amount must be positive: {action.amount}")
        if action.direction not in _VALID_DIRECTIONS:
            raise ValueError(f"Invalid scroll direction: {action.direction!r}")
    elif isinstance(action, GUIDragAction):
        if (
            action.start_x < 0
            or action.start_y < 0
            or action.end_x < 0
            or action.end_y < 0
        ):
            raise ValueError(
                f"Drag coordinates must be non-negative: "
                f"({action.start_x}, {action.start_y}) -> ({action.end_x}, {action.end_y})"
            )
    elif isinstance(action, GUIWaitAction):
        if action.ms <= 0:
            raise ValueError(f"Wait ms must be positive: {action.ms}")
    elif isinstance(action, GUIDoneAction):
        pass  # summary may be empty — signals terminal state regardless
    else:
        raise ValueError(f"Unknown GUIAction type: {type(action).__name__}")


# ---------------------------------------------------------------------------
# parse_gui_response — three parse paths
# ---------------------------------------------------------------------------


def parse_gui_response(response: Any) -> list[GUIAction]:
    """Parse a vision model response into one or more GUIActions.

    Tries three paths in order:
      (a) OpenAI `computer_call` output items (native GPT-5.4 format)
      (b) function_call with `gui_action` tool (non-CU vision models)
      (c) structured text with action keywords (last resort)

    Returns ALL parsed actions from the first path that produces results.
    GPT-5.4 can emit multiple tool_calls per response (e.g. up, up, left)
    and we execute them all before taking the next screenshot.

    Raises ValueError if none of the paths can produce any action.
    """
    actions = (
        _try_parse_computer_call(response)
        or _try_parse_function_call(response)
        or _try_parse_text(response)
        or []
    )
    if not actions:
        raise ValueError(f"Could not parse GUI action from response: {response!r}")
    for action in actions:
        validate_gui_action(action)
    return actions


def _as_dict(obj: Any) -> dict[str, Any] | None:
    """Return obj as a dict-like mapping, handling dicts and pydantic-style objects."""
    if isinstance(obj, dict):
        return obj
    # Support SimpleNamespace-ish / pydantic model objects via attribute access.
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            return None
    return None


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Attribute-or-dict getter."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _try_parse_computer_call(response: Any) -> list[GUIAction] | None:
    """Path (a): OpenAI Responses API computer_call items.

    Handles two sub-formats:
      - singular ``action`` dict (one action per computer_call item)
      - plural ``actions`` array (batch of sequential actions in one item)

    Collects ALL computer_call items in the output (GPT-5.4 can emit multiple).
    """
    output = _get(response, "output")
    if output is None:
        return None
    if not isinstance(output, list):
        return None

    actions: list[GUIAction] = []
    for item in output:
        item_type = _get(item, "type")
        if item_type != "computer_call":
            continue

        # Singular "action" dict
        action_dict = _get(item, "action")
        if action_dict is not None:
            if not isinstance(action_dict, dict):
                action_dict = _as_dict(action_dict)
            if action_dict is not None:
                parsed = _computer_call_to_gui_action(action_dict)
                if parsed is not None:
                    actions.append(parsed)
            continue

        # Plural "actions" array (batch of sequential actions)
        actions_list = _get(item, "actions")
        if isinstance(actions_list, list):
            for entry in actions_list:
                if not isinstance(entry, dict):
                    entry = _as_dict(entry)
                if entry is not None:
                    parsed = _computer_call_to_gui_action(entry)
                    if parsed is not None:
                        actions.append(parsed)

    return actions or None


def _computer_call_to_gui_action(action: dict[str, Any]) -> GUIAction | None:
    """Convert a single OpenAI computer_call action dict to a GUIAction."""
    action_type = action.get("type")
    if action_type == "click":
        button = action.get("button", "left")
        # OpenAI uses "left"/"right"/"wheel"/"back"/"forward" — we only handle left/right.
        if button not in ("left", "right"):
            button = "left"
        return GUIClickAction(x=int(action.get("x", 0)), y=int(action.get("y", 0)), button=button)
    if action_type == "double_click":
        return GUIClickAction(
            x=int(action.get("x", 0)), y=int(action.get("y", 0)), button="double"
        )
    if action_type == "type":
        return GUITypeAction(text=str(action.get("text", "")))
    if action_type == "keypress":
        keys = action.get("keys") or []
        return GUIHotkeyAction(keys=[_normalize_key(str(k)) for k in keys])
    if action_type == "scroll":
        scroll_y = int(action.get("scroll_y", 0))
        scroll_x = int(action.get("scroll_x", 0))
        # Positive scroll_y → content moves down → user scrolls down.
        # Use explicit "direction" if present, else derive from signs.
        if "direction" in action:
            direction = str(action.get("direction", "down"))
        else:
            if scroll_y != 0:
                direction = "down" if scroll_y > 0 else "up"
            elif scroll_x != 0:
                direction = "down" if scroll_x > 0 else "up"
            else:
                direction = "down"
        amount = int(action.get("amount") or max(abs(scroll_x), abs(scroll_y)) or 3)
        return GUIScrollAction(
            x=int(action.get("x", 0)),
            y=int(action.get("y", 0)),
            direction=direction,
            amount=max(amount, 1),
        )
    if action_type == "drag":
        path = action.get("path") or []
        if len(path) < 2:
            return None
        start = path[0]
        end = path[-1]
        return GUIDragAction(
            start_x=int(start.get("x", 0)),
            start_y=int(start.get("y", 0)),
            end_x=int(end.get("x", 0)),
            end_y=int(end.get("y", 0)),
        )
    if action_type == "wait":
        return GUIWaitAction(ms=int(action.get("ms", 1000)))
    return None


def _try_parse_function_call(response: Any) -> list[GUIAction] | None:
    """Path (b): function_call with `gui_action` tool schema.

    Supports two layouts:
      1. OpenAI-style `output` list containing `function_call` items with
         `{name, arguments}`.
      2. Completion-style `tool_calls` list on the assistant message with
         `{function: {name, arguments}}`.

    Collects ALL gui_action calls (GPT-5.4 can emit multiple tool_calls).
    """
    actions: list[GUIAction] = []

    output = _get(response, "output")
    if isinstance(output, list):
        for item in output:
            if _get(item, "type") != "function_call":
                continue
            if _get(item, "name") != "gui_action":
                continue
            parsed = _parse_gui_action_args(_get(item, "arguments"))
            if parsed is not None:
                actions.append(parsed)
        if actions:
            return actions

    tool_calls = _get(response, "tool_calls")
    if tool_calls is None:
        message = _get(response, "message")
        if message is not None:
            tool_calls = _get(message, "tool_calls")
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            fn = _get(tc, "function")
            if fn is None:
                continue
            if _get(fn, "name") != "gui_action":
                continue
            parsed = _parse_gui_action_args(_get(fn, "arguments"))
            if parsed is not None:
                actions.append(parsed)
    return actions or None


def _parse_gui_action_args(arguments: Any) -> GUIAction | None:
    """Decode JSON args from a `gui_action` function call into a GUIAction."""
    if arguments is None:
        return None
    if isinstance(arguments, str):
        try:
            args = json.loads(arguments)
        except json.JSONDecodeError:
            return None
    elif isinstance(arguments, dict):
        args = arguments
    else:
        return None

    kind = args.get("action_type") or args.get("type")
    if kind == "click":
        return GUIClickAction(
            x=int(args.get("x", 0)),
            y=int(args.get("y", 0)),
            button=str(args.get("button", "left")),
        )
    if kind == "type":
        return GUITypeAction(text=str(args.get("text", "")))
    if kind in ("hotkey", "keypress"):
        keys = args.get("keys") or []
        return GUIHotkeyAction(keys=[_normalize_key(str(k)) for k in keys])
    if kind == "scroll":
        return GUIScrollAction(
            x=int(args.get("x", 0)),
            y=int(args.get("y", 0)),
            direction=str(args.get("direction", "down")),
            amount=int(args.get("amount", 3)),
        )
    if kind == "drag":
        return GUIDragAction(
            start_x=int(args.get("start_x", 0)),
            start_y=int(args.get("start_y", 0)),
            end_x=int(args.get("end_x", 0)),
            end_y=int(args.get("end_y", 0)),
        )
    if kind == "wait":
        return GUIWaitAction(ms=int(args.get("ms", 1000)))
    if kind == "done":
        return GUIDoneAction(summary=str(args.get("summary", "")))
    return None


_TEXT_CLICK_RE = re.compile(
    r"^\s*CLICK\s+(\d+)\s+(\d+)(?:\s+(left|right|double))?\s*$", re.IGNORECASE
)
_TEXT_TYPE_RE = re.compile(r'^\s*TYPE\s+"(.*)"\s*$', re.IGNORECASE | re.DOTALL)
_TEXT_HOTKEY_RE = re.compile(r"^\s*HOTKEY\s+(\S+)\s*$", re.IGNORECASE)
_TEXT_SCROLL_RE = re.compile(r"^\s*SCROLL\s+(up|down)(?:\s+(\d+))?\s*$", re.IGNORECASE)
_TEXT_DRAG_RE = re.compile(
    r"^\s*DRAG\s+(\d+)\s+(\d+)\s*(?:->|to)\s*(\d+)\s+(\d+)\s*$", re.IGNORECASE
)
_TEXT_WAIT_RE = re.compile(r"^\s*WAIT\s+(\d+)\s*$", re.IGNORECASE)
_TEXT_DONE_RE = re.compile(r"^\s*DONE(?:[:\s]+(.*))?$", re.IGNORECASE | re.DOTALL)


def _try_parse_text(response: Any) -> list[GUIAction] | None:
    """Path (c): structured text with action keywords.

    Searches for a response text body, then parses ALL keyword lines.
    """
    text = _extract_text(response)
    if text is None:
        return None

    actions: list[GUIAction] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        m = _TEXT_CLICK_RE.match(line)
        if m:
            x, y, btn = int(m.group(1)), int(m.group(2)), (m.group(3) or "left").lower()
            actions.append(GUIClickAction(x=x, y=y, button=btn))
            continue

        m = _TEXT_TYPE_RE.match(line)
        if m:
            actions.append(GUITypeAction(text=m.group(1)))
            continue

        m = _TEXT_HOTKEY_RE.match(line)
        if m:
            keys = [k.strip() for k in m.group(1).split("+") if k.strip()]
            actions.append(GUIHotkeyAction(keys=keys))
            continue

        m = _TEXT_SCROLL_RE.match(line)
        if m:
            direction = m.group(1).lower()
            amount = int(m.group(2)) if m.group(2) else 3
            actions.append(GUIScrollAction(x=0, y=0, direction=direction, amount=amount))
            continue

        m = _TEXT_DRAG_RE.match(line)
        if m:
            actions.append(GUIDragAction(
                start_x=int(m.group(1)),
                start_y=int(m.group(2)),
                end_x=int(m.group(3)),
                end_y=int(m.group(4)),
            ))
            continue

        m = _TEXT_WAIT_RE.match(line)
        if m:
            actions.append(GUIWaitAction(ms=int(m.group(1))))
            continue

        m = _TEXT_DONE_RE.match(line)
        if m:
            summary = (m.group(1) or "").strip()
            actions.append(GUIDoneAction(summary=summary))
            continue

    return actions or None

    return None


def _extract_text(response: Any) -> str | None:
    """Pull a flat text string from assorted response shapes."""
    if isinstance(response, str):
        return response

    # OpenAI Responses-style: output[].content[].text
    output = _get(response, "output")
    if isinstance(output, list):
        chunks: list[str] = []
        for item in output:
            content = _get(item, "content")
            if isinstance(content, list):
                for c in content:
                    text = _get(c, "text")
                    if isinstance(text, str):
                        chunks.append(text)
            elif isinstance(content, str):
                chunks.append(content)
        if chunks:
            return "\n".join(chunks)

    # Completion-style: message.content
    message = _get(response, "message")
    if message is not None:
        content = _get(message, "content")
        if isinstance(content, str):
            return content

    # Top-level .content
    content = _get(response, "content")
    if isinstance(content, str):
        return content

    return None


# ---------------------------------------------------------------------------
# execute_gui_action — dispatch GUIAction to RemoteDesktopSession methods
# ---------------------------------------------------------------------------


async def execute_gui_action(action: GUIAction, session: Any) -> str | None:
    """Execute a GUIAction against a RemoteDesktopSession.

    Returns:
        None for executed actions (click/type/hotkey/scroll/drag/wait).
        The summary string for GUIDoneAction (no VM call made).

    Raises ValueError for unknown action types.
    """
    if isinstance(action, GUIDoneAction):
        return action.summary

    if isinstance(action, GUIClickAction):
        if action.button == "left":
            await session.click(action.x, action.y)
        elif action.button == "right":
            await session.right_click(action.x, action.y)
        elif action.button == "double":
            await session.double_click(action.x, action.y)
        else:
            raise ValueError(f"Unknown click button: {action.button!r}")
        return None

    if isinstance(action, GUITypeAction):
        await session.type(action.text)
        return None

    if isinstance(action, GUIHotkeyAction):
        await session.hotkey(action.keys)
        return None

    if isinstance(action, GUIScrollAction):
        # session.scroll(direction, amount) — position is determined server-side.
        await session.scroll(action.direction, action.amount)
        return None

    if isinstance(action, GUIDragAction):
        await session.drag(action.start_x, action.start_y, action.end_x, action.end_y)
        return None

    if isinstance(action, GUIWaitAction):
        await asyncio.sleep(action.ms / 1000.0)
        return None

    raise ValueError(f"Unknown GUIAction type: {type(action).__name__}")


# ---------------------------------------------------------------------------
# Tool schema for non-CU vision models
# ---------------------------------------------------------------------------


def gui_action_tool_schema() -> dict[str, Any]:
    """OpenAI function-calling schema for the `gui_action` tool.

    Used in parse path (b) so non-CU vision models can emit structured actions
    via standard function calling instead of the native computer_call format.
    """
    return {
        "type": "function",
        "function": {
            "name": "gui_action",
            "description": (
                "Emit the next GUI automation action given the current screenshot "
                "and instruction. Use action_type=done when the task is complete."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action_type": {
                        "type": "string",
                        "enum": [
                            "click",
                            "type",
                            "keypress",
                            "scroll",
                            "drag",
                            "wait",
                            "done",
                        ],
                        "description": "Which action to perform.",
                    },
                    "x": {"type": "integer", "description": "X coordinate for click/scroll."},
                    "y": {"type": "integer", "description": "Y coordinate for click/scroll."},
                    "button": {
                        "type": "string",
                        "enum": ["left", "right", "double"],
                        "description": "Click button (default left).",
                    },
                    "text": {"type": "string", "description": "Text for type action."},
                    "keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Keys to press (for action=keypress). Example: ['ctrl', 'c'] for combo, ['down'] for single key.",
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["up", "down"],
                        "description": "Scroll direction.",
                    },
                    "amount": {
                        "type": "integer",
                        "description": "Scroll amount (default 3).",
                    },
                    "start_x": {"type": "integer", "description": "Drag start X."},
                    "start_y": {"type": "integer", "description": "Drag start Y."},
                    "end_x": {"type": "integer", "description": "Drag end X."},
                    "end_y": {"type": "integer", "description": "Drag end Y."},
                    "ms": {"type": "integer", "description": "Wait duration in ms."},
                    "summary": {
                        "type": "string",
                        "description": "Summary of what was accomplished (for done).",
                    },
                },
                "required": ["action_type"],
            },
        },
    }


__all__ = [
    "GUIAction",
    "GUIClickAction",
    "GUIDoneAction",
    "GUIDragAction",
    "GUIHotkeyAction",
    "GUIScrollAction",
    "GUITypeAction",
    "GUIWaitAction",
    "execute_gui_action",
    "gui_action_tool_schema",
    "parse_gui_response",
    "validate_gui_action",
]
