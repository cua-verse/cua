"""Canonical internal message format for the OpenClaw agent harness.

Defines typed role-based messages and content blocks that serve as the single
internal representation for all pipeline passes (repair, sanitization,
compaction output, format conversion).

Design follows OpenClaw's AgentMessage pattern:
  - Role-based messages with typed content block arrays
  - stop_reason at the message level (needed by repair passes)
  - Content is always a list (string content normalized at ingestion)
  - actions is always a list (singular action normalized at ingestion)

Field conventions match OpenClaw / Anthropic:
  - ``id`` on FunctionCallBlock / ComputerCallBlock (not ``call_id``)
  - ``tool_use_id`` on ToolResultBlock
  - ``call_id`` is Responses API only — adapters map ``id`` → ``call_id``

US-OC-038: Canonical Internal Message Format.

Reference:
  - openclaw/src/agents/pi-embedded-runner/google.ts — sanitizeSessionHistory pipeline
  - openclaw/src/agents/session-transcript-repair.ts — repair passes on AgentMessage[]
  - session.py:819-1080 — convert_to_responses_api_items (pattern reference)
"""

from __future__ import annotations

import json
from typing import Any, Literal, Union

from typing_extensions import NotRequired, TypedDict

# ---------------------------------------------------------------------------
# Content block types
# ---------------------------------------------------------------------------


class TextBlock(TypedDict):
    """Plain text content block."""

    type: Literal["text"]
    text: str


class FunctionCallBlock(TypedDict):
    """Function (tool) call issued by the assistant."""

    type: Literal["function_call"]
    id: str
    name: str
    arguments: str  # JSON string


class ComputerCallBlock(TypedDict):
    """Computer action call issued by the assistant.

    ``actions`` is always a list — singular ``action`` dicts are normalized
    to ``[action]`` at ingestion by :func:`normalize_to_canonical`.
    """

    type: Literal["computer_call"]
    id: str
    actions: list[dict[str, Any]]


class ToolResultBlock(TypedDict):
    """Result of a function or computer call."""

    type: Literal["tool_result"]
    tool_use_id: str
    content: str
    is_error: NotRequired[bool]


class ThinkingBlock(TypedDict):
    """Provider-specific reasoning / thinking block.

    ``thinkingSignature`` is a tamper-proof token validated by the API on
    re-submission — if missing, malformed, or from a different provider the
    API rejects the request.
    """

    type: Literal["thinking"]
    thinking: str
    thinkingSignature: NotRequired[str]


class CompactionSummaryBlock(TypedDict):
    """Summary of compacted (older) conversation history.

    Distinct from TextBlock so downstream passes can identify and skip
    compaction summaries during repair / sanitization.
    """

    type: Literal["compaction_summary"]
    text: str


ContentBlock = Union[
    TextBlock,
    FunctionCallBlock,
    ComputerCallBlock,
    ToolResultBlock,
    ThinkingBlock,
    CompactionSummaryBlock,
]

# ---------------------------------------------------------------------------
# Canonical message
# ---------------------------------------------------------------------------


class CanonicalMessage(TypedDict):
    """Role-based message with typed content blocks.

    Mirrors OpenClaw's AgentMessage: role + content array + optional
    stop_reason.  All pipeline passes (repair, sanitization, format
    conversion) operate on ``list[CanonicalMessage]``.
    """

    role: Literal["user", "assistant", "tool", "system"]
    content: list[ContentBlock]
    stop_reason: NotRequired[str]


# ---------------------------------------------------------------------------
# Compaction summary preamble (used by adapters)
# ---------------------------------------------------------------------------

COMPACTION_PREAMBLE = (
    "## Prior Context (Compacted)\n"
    "The following is a summary of earlier conversation history that was "
    "compacted to save context space. Use this to maintain continuity.\n\n"
)

# ---------------------------------------------------------------------------
# Ingestion: untyped dicts → canonical
# ---------------------------------------------------------------------------


def normalize_to_canonical(
    messages: list[dict[str, Any]],
) -> list[CanonicalMessage]:
    """Convert untyped role-based dicts to typed canonical messages.

    Normalizes:
      - String content → ``[TextBlock]``
      - ``action: {…}`` → ``actions: [{…}]``
      - Preserves ``stop_reason`` on messages that have it
    """
    result: list[CanonicalMessage] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        blocks = _normalize_content(content, role)
        canonical: CanonicalMessage = {"role": role, "content": blocks}
        stop_reason = msg.get("stop_reason")
        if stop_reason:
            canonical["stop_reason"] = stop_reason
        result.append(canonical)
    return result


def _normalize_content(
    content: Any, role: str
) -> list[ContentBlock]:
    """Normalize a message's content field to a list of typed ContentBlocks."""
    if isinstance(content, str):
        return [TextBlock(type="text", text=content)]

    if not isinstance(content, list):
        return [TextBlock(type="text", text=str(content))]

    blocks: list[ContentBlock] = []
    for block in content:
        if not isinstance(block, dict):
            blocks.append(TextBlock(type="text", text=str(block)))
            continue

        btype = block.get("type", "")

        if btype == "text":
            blocks.append(TextBlock(type="text", text=block.get("text", "")))

        elif btype == "function_call":
            blocks.append(FunctionCallBlock(
                type="function_call",
                id=block.get("id", block.get("call_id", "")),
                name=block.get("name", ""),
                arguments=block.get("arguments", ""),
            ))

        elif btype == "computer_call":
            blocks.append(ComputerCallBlock(
                type="computer_call",
                id=block.get("id", block.get("call_id", "")),
                actions=_normalize_actions(block),
            ))

        elif btype == "tool_result":
            tb = ToolResultBlock(
                type="tool_result",
                tool_use_id=block.get("tool_use_id", block.get("call_id", "")),
                content=block.get("content", ""),
            )
            if block.get("is_error"):
                tb["is_error"] = True
            blocks.append(tb)

        elif btype == "computer_call_output":
            # Stored as a tool_result-like block in some transcript paths.
            # Normalize to ToolResultBlock with the original content.
            tb = ToolResultBlock(
                type="tool_result",
                tool_use_id=block.get("call_id", block.get("tool_use_id", "")),
                content=block.get("output", block.get("content", "")),
            )
            blocks.append(tb)

        elif btype == "thinking":
            tb_thinking = ThinkingBlock(
                type="thinking",
                thinking=block.get("thinking", ""),
            )
            sig = block.get("thinkingSignature")
            if sig:
                tb_thinking["thinkingSignature"] = sig
            blocks.append(tb_thinking)

        else:
            # Unknown block type — preserve as text
            text = block.get("text", block.get("content", str(block)))
            blocks.append(TextBlock(type="text", text=str(text)))

    return blocks


def _normalize_actions(block: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize action/actions to always be a list."""
    actions = block.get("actions")
    if isinstance(actions, list):
        return actions
    action = block.get("action")
    if action is not None:
        return [action]
    return []


# ---------------------------------------------------------------------------
# Adapter: canonical → OpenAI Responses API flat items
# ---------------------------------------------------------------------------


def canonical_to_responses_api(
    messages: list[CanonicalMessage],
) -> list[dict[str, Any]]:
    """Convert canonical messages to OpenAI Responses API flat items.

    Each canonical message is unnested into one or more flat items:
      - User TextBlock → ``{type: "message", role: "user", …}``
      - Assistant TextBlock → ``{type: "message", role: "assistant", …}``
      - FunctionCallBlock → ``{type: "function_call", call_id: …}``
      - ComputerCallBlock → ``{type: "computer_call", call_id: …}``
      - ToolResultBlock → ``function_call_output`` or ``computer_call_output``
      - CompactionSummaryBlock → user message with preamble
      - ThinkingBlock → skipped (not representable in Responses API items)
    """
    items: list[dict[str, Any]] = []
    # Track call types so tool results emit the correct output type
    call_type_map: dict[str, str] = {}

    for msg in messages:
        role = msg["role"]
        for block in msg["content"]:
            btype = block["type"]

            if btype == "compaction_summary":
                items.append({
                    "type": "message",
                    "role": "user",
                    "content": [{
                        "type": "input_text",
                        "text": COMPACTION_PREAMBLE + block["text"],
                    }],
                })

            elif btype == "text":
                if role == "assistant":
                    items.append({
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": block["text"]}],
                    })
                else:
                    items.append({
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": block["text"]}],
                    })

            elif btype == "function_call":
                call_id = block["id"]
                call_type_map[call_id] = "function_call"
                items.append({
                    "type": "function_call",
                    "call_id": call_id,
                    "name": block["name"],
                    "arguments": block["arguments"],
                })

            elif btype == "computer_call":
                call_id = block["id"]
                call_type_map[call_id] = "computer_call"
                # Compacted/replayed computer_call blocks no longer have
                # the original screenshot. Convert to text — OpenAI validates
                # image data in computer_call_output and rejects placeholders.
                # Matches _normalize_messages_for_gpt54 behavior in openai.py.
                actions = block["actions"]
                action_desc = json.dumps(actions)[:200] if actions else "details unavailable"
                text_type = "output_text" if role == "assistant" else "input_text"
                items.append({
                    "type": "message",
                    "role": role if role != "tool" else "user",
                    "content": [{
                        "type": text_type,
                        "text": f"[computer action: {action_desc}]",
                    }],
                })

            elif btype == "tool_result":
                call_id = block["tool_use_id"]
                if call_type_map.get(call_id) == "computer_call":
                    # Computer call result after compaction — screenshot is
                    # gone. Convert to text (matching computer_call branch
                    # above). No call/result pairing issue since both are text.
                    items.append({
                        "type": "message",
                        "role": "user",
                        "content": [{
                            "type": "input_text",
                            "text": f"[computer result: {block['content'][:200]}]",
                        }],
                    })
                else:
                    items.append({
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": block["content"],
                    })

            elif btype == "thinking":
                # Thinking blocks are not representable in Responses API items.
                # Skipped — US-OC-041 will add thinking sanitization passes.
                pass

    return _ensure_tool_adjacency(items)


def _ensure_tool_adjacency(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reorder items so each tool call is immediately followed by its output.

    Defers non-output items that appear between a call and its matching output,
    then flushes them after the output. Matches the algorithm in session.py.
    """
    result: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    pending_call_ids: set[str] = set()

    for item in items:
        t = item.get("type", "")

        if t in ("function_call", "computer_call"):
            pending_call_ids.add(item.get("call_id", ""))
            result.append(item)
        elif t in ("function_call_output", "computer_call_output"):
            call_id = item.get("call_id", "")
            pending_call_ids.discard(call_id)
            result.append(item)
            if not pending_call_ids:
                result.extend(deferred)
                deferred = []
        elif pending_call_ids:
            deferred.append(item)
        else:
            result.append(item)

    result.extend(deferred)
    return result


# ---------------------------------------------------------------------------
# Adapter: canonical → Anthropic completion messages
# ---------------------------------------------------------------------------


def canonical_to_anthropic_messages(
    messages: list[CanonicalMessage],
) -> list[dict[str, Any]]:
    """Convert canonical messages to Anthropic completion format.

    Groups content blocks by role into role-based messages:
      - FunctionCallBlock → ``{type: "tool_use", id, name, input}``
      - ComputerCallBlock → ``{type: "tool_use", id, name: "computer", input}``
      - ToolResultBlock → ``{role: "tool", content: [{type: "tool_result", …}]}``
      - CompactionSummaryBlock → user text with preamble
      - ThinkingBlock → ``{type: "thinking", thinking, signature}``

    Consecutive blocks within the same message are grouped. Tool messages
    break the grouping to ensure correct Anthropic turn structure.
    """
    result: list[dict[str, Any]] = []

    for msg in messages:
        role = msg["role"]

        if role == "tool":
            # Tool messages: each ToolResultBlock becomes an Anthropic tool_result
            tool_content: list[dict[str, Any]] = []
            for block in msg["content"]:
                if block["type"] == "tool_result":
                    tr: dict[str, Any] = {
                        "type": "tool_result",
                        "tool_use_id": block["tool_use_id"],
                        "content": block["content"],
                    }
                    if block.get("is_error"):
                        tr["is_error"] = True
                    tool_content.append(tr)
            if tool_content:
                result.append({"role": "user", "content": tool_content})

        elif role == "assistant":
            # Assistant messages: map blocks to Anthropic content types
            content: list[dict[str, Any]] = []
            for block in msg["content"]:
                btype = block["type"]
                if btype == "text":
                    content.append({"type": "text", "text": block["text"]})
                elif btype == "function_call":
                    content.append({
                        "type": "tool_use",
                        "id": block["id"],
                        "name": block["name"],
                        "input": json.loads(block["arguments"]) if block["arguments"] else {},
                    })
                elif btype == "computer_call":
                    content.append({
                        "type": "tool_use",
                        "id": block["id"],
                        "name": "computer",
                        "input": {"actions": block["actions"]},
                    })
                elif btype == "thinking":
                    tb: dict[str, Any] = {
                        "type": "thinking",
                        "thinking": block["thinking"],
                    }
                    if block.get("thinkingSignature"):
                        tb["signature"] = block["thinkingSignature"]
                    content.append(tb)
                elif btype == "compaction_summary":
                    content.append({"type": "text", "text": COMPACTION_PREAMBLE + block["text"]})
            if content:
                result.append({"role": "assistant", "content": content})

        else:
            # User / system messages
            content_out: list[dict[str, Any]] = []
            for block in msg["content"]:
                btype = block["type"]
                if btype == "text":
                    content_out.append({"type": "text", "text": block["text"]})
                elif btype == "compaction_summary":
                    content_out.append({
                        "type": "text",
                        "text": COMPACTION_PREAMBLE + block["text"],
                    })
                else:
                    content_out.append({"type": "text", "text": str(block)})
            if content_out:
                result.append({"role": role, "content": content_out})

    return result
