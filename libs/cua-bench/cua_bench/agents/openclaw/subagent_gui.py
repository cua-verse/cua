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
import json
from datetime import datetime, timezone
from pathlib import Path
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
        "- You may emit one or multiple `gui_action` tool calls per turn.",
        "- Multiple actions execute sequentially; the next screenshot arrives after all complete.",
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
        "3. **Observe before repeating** — Before repeating the same action, "
        "check the latest screenshot to confirm it had the intended effect.",
        "4. **Be ephemeral** — You will be terminated after returning. That's fine.",
        "",
        "## Output Format",
        "- One `gui_action` function call per turn.",
        "- No free-text narration; the tool call is the entire response.",
    ])


# ---------------------------------------------------------------------------
# Action formatting (human description)
# ---------------------------------------------------------------------------


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
# Transcript persistence
# ---------------------------------------------------------------------------


class _TranscriptWriter:
    """Lightweight append-only JSONL writer for GUI subagent turns.

    Each entry records one relay turn: the actions taken and the step number.
    Screenshots are NOT stored (too large for JSONL); the transcript captures
    the action sequence and model responses only.
    """

    def __init__(self, transcript_path: Path | None) -> None:
        self._path = transcript_path

    def _append(self, entry: dict[str, Any]) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def write_turn(
        self, step: int, actions: list[str], raw_choice: Any, is_done: bool
    ) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        # Extract tool_calls arguments from the raw choice for replay/debugging
        message = getattr(raw_choice, "message", None)
        tool_calls_raw = None
        if message is not None:
            tcs = getattr(message, "tool_calls", None)
            if tcs:
                tool_calls_raw = []
                for tc in tcs:
                    fn = getattr(tc, "function", None)
                    if fn:
                        tool_calls_raw.append({
                            "id": getattr(tc, "id", ""),
                            "name": getattr(fn, "name", ""),
                            "arguments": getattr(fn, "arguments", ""),
                        })
        self._append({
            "type": "turn",
            "step": step,
            "timestamp": ts,
            "actions": actions,
            "is_done": is_done,
            "tool_calls": tool_calls_raw,
        })


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


def _tool_result_messages(choice: Any, result: str) -> list[dict[str, Any]]:
    """Build tool-result messages for each tool_call in the assistant turn.

    Returns an empty list if the choice has no tool_calls (text-fallback path).
    """
    message = getattr(choice, "message", None)
    tool_calls = None if message is None else getattr(message, "tool_calls", None)
    if not tool_calls:
        return []
    return [
        {"role": "tool", "tool_call_id": getattr(tc, "id", ""), "content": result}
        for tc in tool_calls
    ]


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
    parent_session_dir: str | Path | None = None,
) -> str:
    """Blocking vision-to-action relay loop.

    Each iteration:
      1. Call ``litellm.acompletion`` with the current messages + gui_action tool.
      2. Parse the response into one or more ``GUIAction`` via ``parse_gui_response``.
      3. If any action is ``GUIDoneAction``: execute preceding actions, then
         complete the run and return the summary.
      4. Otherwise: execute all actions on ``session``, take a fresh screenshot,
         append it to messages, prune image history, loop.

    GPT-5.4 can emit multiple tool_calls per response (e.g. up, up, up, done).
    All actions are executed sequentially before the next screenshot — matching
    how CUA's main agent loop handles multi-action responses.

    Terminates on ``GUIDoneAction``, ``max_steps`` reached, or any LLM/session
    exception (re-raised after ``registry.fail``).

    Returns the final summary string. Raises the underlying exception on failure.
    """
    import litellm

    transcript_path: Path | None = None
    if parent_session_dir is not None:
        transcript_path = Path(parent_session_dir) / "subagents" / run_id / "transcript.jsonl"
    transcript = _TranscriptWriter(transcript_path)

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
            actions = parse_gui_response(choice)
        except ValueError as e:
            registry.fail(run_id, f"parse error: {e}", usage)
            raise

        # Split at first GUIDoneAction — execute everything before it.
        to_execute: list[GUIAction] = []
        done_action: GUIDoneAction | None = None
        for a in actions:
            if isinstance(a, GUIDoneAction):
                done_action = a
                break
            to_execute.append(a)

        # Debug: log actions for this step
        descs = [_describe_action(a) for a in to_execute]
        if done_action:
            descs.append(f"done: {done_action.summary}")
        print(f"  [GUI {run_id}] step {_step}: {descs}")

        # Persist turn to disk transcript.
        transcript.write_turn(_step, descs, choice, is_done=done_action is not None)

        # Record the assistant turn + tool acks in transcript.
        messages.append(_assistant_tool_call_message(choice))
        messages.extend(_tool_result_messages(choice, "ok"))

        # Execute all non-done actions sequentially.
        last_action_desc = ""
        for action in to_execute:
            try:
                await execute_gui_action(action, session)
                last_action_desc = _describe_action(action)
            except Exception as e:
                registry.fail(run_id, f"execute error: {e}", usage)
                raise

        if done_action is not None:
            summary = done_action.summary or "(no summary)"
            registry.complete(run_id, summary, usage)
            return summary

        # Take one screenshot after the entire batch.
        action_desc = (
            last_action_desc
            if len(to_execute) == 1
            else f"{len(to_execute)} actions (last: {last_action_desc})"
        )
        try:
            new_screenshot = await session.screenshot()
        except Exception as e:
            registry.fail(run_id, f"screenshot failed: {e}", usage)
            raise

        messages.append(
            _user_screenshot_message(
                new_screenshot,
                f"After {action_desc}. Current screenshot:",
            )
        )
        _prune_images_to_last_n(messages, n=DEFAULT_IMAGE_HISTORY)
    else:
        partial = f"max_steps ({max_steps}) reached without completion"
        registry.complete(run_id, partial, usage)
        return partial

    return ""  # unreachable — both for/else branches return; satisfies type checkers


__all__ = [
    "DEFAULT_IMAGE_HISTORY",
    "DEFAULT_MAX_STEPS",
    "DEFAULT_MODEL",
    "run_gui_subagent",
]
