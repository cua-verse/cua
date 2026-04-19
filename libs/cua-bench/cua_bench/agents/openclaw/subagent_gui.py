"""GUI subagent — blocking vision-to-action relay loop.

Consumes the protocol layer from ``subagent_gui_protocol`` (GUIAction types,
parse_gui_response, execute_gui_action, gui_action_tool_schema) and drives a
vision model through a take-screenshot → call → parse → execute → repeat
loop until the model emits ``GUIDoneAction`` or a hard limit trips.

Single request path: ``litellm.acompletion`` with the ``gui_action`` function-
calling tool. CUA's ``agent/loops/openai.py:137-141`` confirms GPT-5.4 does
not support native ``computer_use_preview``; all vision models go through
this one path. A future story can add a native Responses-API branch when a
``computer-use-preview`` model is available for end-to-end testing.

Design adapted from OpenClaw:
  - subagent-announce.ts:47-104 (buildSubagentSystemPrompt — role, rules,
    output format, ephemeral framing)
  - subagent-registry.ts (register → mark_running → complete/fail lifecycle)
Simplified for CUA's single-process asyncio model: no gateway, no streaming,
no nested tool inheritance, no depth tracking.
"""

from __future__ import annotations

import base64
from typing import Any

from .subagent_gui_protocol import (
    GUIAction,
    GUIClickAction,
    GUIDoneAction,
    GUIDragAction,
    GUIHotkeyAction,
    GUIScrollAction,
    GUITypeAction,
    GUIWaitAction,
    execute_gui_action,
    gui_action_tool_schema,
    parse_gui_response,
)
from .subagent_registry import SubagentRegistry, SubagentUsage

DEFAULT_MAX_STEPS = 15
DEFAULT_MODEL = "openrouter/openai/gpt-5.4"
DEFAULT_IMAGE_HISTORY = 3
DEDUP_WINDOW = 3


class StuckInLoopError(RuntimeError):
    """Raised when the subagent emits the same action ``DEDUP_WINDOW`` times in a row."""


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


def _build_system_prompt() -> str:
    """Build the GUI subagent system prompt.

    Reference: openclaw/src/agents/subagent-announce.ts:47-104
    """
    return "\n".join([
        "# GUI Subagent",
        "",
        "You are a **GUI automation subagent** spawned by the main agent to perform "
        "a focused GUI task on a Windows VM.",
        "",
        "## Visibility Model",
        "- Each turn, you receive the current VM screenshot.",
        "- You do not have a memory of past turns beyond the action you just emitted.",
        "- Emit exactly one action per turn via the `gui_action` tool call.",
        "",
        "## Action Vocabulary",
        "- `click` — click at (x, y) with button left/right/double.",
        "- `type` — type literal text at the current focus.",
        "- `hotkey` — press a key combination (e.g. ['ctrl', 'c']).",
        "- `scroll` — scroll the page up or down by `amount` notches.",
        "- `drag` — drag from (start_x, start_y) to (end_x, end_y).",
        "- `wait` — wait for `ms` milliseconds (use for UI to settle).",
        "- `done` — the task is complete; include a one-line `summary`.",
        "",
        "## Rules",
        "1. **Stay focused** — Do only the assigned task, no side quests.",
        "2. **Complete and return** — Emit `done` with a summary as soon as the "
        "task is visibly complete on screen.",
        "3. **Don't repeat yourself** — If an action didn't work, try something "
        "different. Identical actions repeated three times in a row abort the run.",
        "4. **Be ephemeral** — You will be terminated after returning. That's fine.",
        "",
        "## Output Format",
        "- One `gui_action` function call per turn.",
        "- No free-text narration; the tool call is the entire response.",
    ])


# ---------------------------------------------------------------------------
# Action formatting (dedup key + human description)
# ---------------------------------------------------------------------------


def _action_key(action: GUIAction) -> tuple:
    """Stable hashable key for dedup. GUIWaitAction is intentionally excluded
    by the caller — polling is a legitimate repeated pattern."""
    if isinstance(action, GUIClickAction):
        return ("click", action.x, action.y, action.button)
    if isinstance(action, GUITypeAction):
        return ("type", action.text)
    if isinstance(action, GUIHotkeyAction):
        return ("hotkey", tuple(action.keys))
    if isinstance(action, GUIScrollAction):
        return ("scroll", action.x, action.y, action.direction, action.amount)
    if isinstance(action, GUIDragAction):
        return ("drag", action.start_x, action.start_y, action.end_x, action.end_y)
    if isinstance(action, GUIWaitAction):
        return ("wait", action.ms)
    if isinstance(action, GUIDoneAction):
        return ("done", action.summary)
    return (type(action).__name__,)


def _describe_action(action: GUIAction) -> str:
    """One-line human-readable description used in per-turn text."""
    if isinstance(action, GUIClickAction):
        return f"click ({action.x}, {action.y}) {action.button}"
    if isinstance(action, GUITypeAction):
        return f"type {action.text!r}"
    if isinstance(action, GUIHotkeyAction):
        return f"hotkey {'+'.join(action.keys)}"
    if isinstance(action, GUIScrollAction):
        return f"scroll {action.direction} {action.amount}"
    if isinstance(action, GUIDragAction):
        return (
            f"drag ({action.start_x}, {action.start_y}) -> "
            f"({action.end_x}, {action.end_y})"
        )
    if isinstance(action, GUIWaitAction):
        return f"wait {action.ms}ms"
    if isinstance(action, GUIDoneAction):
        return f"done: {action.summary}" if action.summary else "done"
    return type(action).__name__


# ---------------------------------------------------------------------------
# Screenshot encoding + history pruning
# ---------------------------------------------------------------------------


def _encode_screenshot(png_bytes: bytes) -> dict[str, Any]:
    """Encode PNG bytes as an OpenAI-compatible image_url content block."""
    b64 = base64.b64encode(png_bytes).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{b64}"},
    }


_IMAGE_PLACEHOLDER = {"type": "text", "text": "[older screenshot omitted]"}


def _prune_images_to_last_n(
    messages: list[dict[str, Any]], n: int = DEFAULT_IMAGE_HISTORY
) -> None:
    """Keep only the last ``n`` image blocks across all messages; replace older
    ones with a text placeholder. Mutates ``messages`` in place.

    Matches ``only_n_most_recent_images=3`` semantics used by the main agent.
    """
    if n < 0:
        n = 0
    kept = 0
    # Walk newest → oldest, keep first n image blocks, replace rest.
    for msg in reversed(messages):
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for i, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            if block.get("type") != "image_url":
                continue
            if kept < n:
                kept += 1
            else:
                content[i] = dict(_IMAGE_PLACEHOLDER)


# ---------------------------------------------------------------------------
# Usage accumulation
# ---------------------------------------------------------------------------


def _accumulate_usage(usage: SubagentUsage, response: Any) -> None:
    """Add prompt+completion tokens from a litellm response to ``usage``."""
    resp_usage = getattr(response, "usage", None)
    if resp_usage is None:
        return
    usage.input_tokens += int(getattr(resp_usage, "prompt_tokens", 0) or 0)
    usage.output_tokens += int(getattr(resp_usage, "completion_tokens", 0) or 0)


# ---------------------------------------------------------------------------
# Message construction helpers
# ---------------------------------------------------------------------------


def _assistant_tool_call_message(choice: Any) -> dict[str, Any]:
    """Build the assistant message that records a tool_call turn.

    Works for both native litellm objects (choice.message.tool_calls) and our
    dataclass-style mocks. Returns a message with an empty content string and
    a tool_calls list faithful to what the model emitted, or — if the model
    didn't actually emit tool_calls (text-fallback path) — a text-only
    assistant message.
    """
    message = getattr(choice, "message", None)
    content = "" if message is None else (getattr(message, "content", None) or "")
    tool_calls = None if message is None else getattr(message, "tool_calls", None)

    if not tool_calls:
        return {"role": "assistant", "content": content}

    serialized: list[dict[str, Any]] = []
    for tc in tool_calls:
        fn = getattr(tc, "function", None)
        if fn is None:
            continue
        serialized.append({
            "id": getattr(tc, "id", ""),
            "type": "function",
            "function": {
                "name": getattr(fn, "name", ""),
                "arguments": getattr(fn, "arguments", ""),
            },
        })
    return {"role": "assistant", "content": content, "tool_calls": serialized}


def _tool_result_message(choice: Any, result: str) -> dict[str, Any] | None:
    """Build the tool-result message paired with the assistant's tool_call.

    Returns ``None`` if the choice has no tool_calls (text-fallback path —
    then the assistant message stands alone without a tool reply).
    """
    message = getattr(choice, "message", None)
    tool_calls = None if message is None else getattr(message, "tool_calls", None)
    if not tool_calls:
        return None
    first = tool_calls[0]
    call_id = getattr(first, "id", "")
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": result,
    }


def _user_screenshot_message(png_bytes: bytes, prefix: str) -> dict[str, Any]:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": prefix},
            _encode_screenshot(png_bytes),
        ],
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_gui_subagent(
    *,
    instruction: str,
    session: Any,
    registry: SubagentRegistry,
    run_id: str,
    model: str = DEFAULT_MODEL,
    max_steps: int = DEFAULT_MAX_STEPS,
    thinking_params: dict[str, Any] | None = None,
) -> str:
    """Blocking vision-to-action relay loop.

    Each iteration:
      1. Call ``litellm.acompletion`` with the current messages + gui_action tool.
      2. Parse the response into a ``GUIAction`` via ``parse_gui_response``.
      3. If ``GUIDoneAction``: complete the run and return the summary.
      4. Otherwise: execute the action on ``session``, take a fresh screenshot,
         append it to messages, prune image history, loop.

    Terminates on ``GUIDoneAction``, ``max_steps`` reached, any LLM/session
    exception (re-raised after ``registry.fail``), or three consecutive
    identical actions (``StuckInLoopError``).

    Returns the final summary string. Raises ``StuckInLoopError`` or the
    underlying exception on failure.
    """
    import litellm

    usage = SubagentUsage()
    registry.mark_running(run_id)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _build_system_prompt()},
        {"role": "user", "content": instruction},
    ]

    try:
        initial_screenshot = await session.screenshot()
    except Exception as e:
        registry.fail(run_id, f"initial screenshot failed: {e}", usage)
        raise

    messages.append(_user_screenshot_message(initial_screenshot, "Current screenshot:"))

    action_keys: list[tuple] = []

    for _step in range(max_steps):
        try:
            response = await litellm.acompletion(
                model=model,
                messages=messages,
                tools=[gui_action_tool_schema()],
                max_tokens=1024,
                temperature=1.0,
                **(thinking_params or {}),
            )
        except Exception as e:
            registry.fail(run_id, str(e), usage)
            raise

        _accumulate_usage(usage, response)

        try:
            choice = response.choices[0]
        except (AttributeError, IndexError) as e:
            registry.fail(run_id, f"empty response: {e}", usage)
            raise

        try:
            action = parse_gui_response(choice)
        except ValueError as e:
            registry.fail(run_id, f"parse error: {e}", usage)
            raise

        if isinstance(action, GUIDoneAction):
            summary = action.summary or "(no summary)"
            registry.complete(run_id, summary, usage)
            return summary

        if not isinstance(action, GUIWaitAction):
            action_keys.append(_action_key(action))
            if (
                len(action_keys) >= DEDUP_WINDOW
                and len(set(action_keys[-DEDUP_WINDOW:])) == 1
            ):
                msg = (
                    f"stuck in loop: action {_describe_action(action)!r} "
                    f"repeated {DEDUP_WINDOW} times"
                )
                registry.fail(run_id, msg, usage)
                raise StuckInLoopError(msg)

        # Record the assistant turn + synthetic tool ack so the next call sees
        # the action in the transcript.
        messages.append(_assistant_tool_call_message(choice))
        tool_ack = _tool_result_message(choice, "ok")
        if tool_ack is not None:
            messages.append(tool_ack)

        try:
            await execute_gui_action(action, session)
        except Exception as e:
            registry.fail(run_id, f"execute error: {e}", usage)
            raise

        try:
            new_screenshot = await session.screenshot()
        except Exception as e:
            registry.fail(run_id, f"screenshot failed: {e}", usage)
            raise

        messages.append(
            _user_screenshot_message(
                new_screenshot,
                f"After action {_describe_action(action)}. Current screenshot:",
            )
        )
        _prune_images_to_last_n(messages, n=DEFAULT_IMAGE_HISTORY)
    else:
        partial = f"max_steps ({max_steps}) reached without completion"
        registry.complete(run_id, partial, usage)
        return partial

    return ""  # unreachable — both for/else branches return; satisfies type checkers


__all__ = [
    "DEDUP_WINDOW",
    "DEFAULT_IMAGE_HISTORY",
    "DEFAULT_MAX_STEPS",
    "DEFAULT_MODEL",
    "StuckInLoopError",
    "run_gui_subagent",
]
