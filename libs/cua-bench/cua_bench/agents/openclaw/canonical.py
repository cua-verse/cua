"""Canonical internal message format and sanitize_items() pipeline.

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
US-OC-039: sanitize_items() pipeline — repair, ordering, format conversion.

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
    """Convert untyped dicts to typed canonical messages.

    Handles two input formats:
      1. **Role-based messages** (``{role, content}``): from compaction, session
         replay, and Anthropic completion format.
      2. **Flat Responses API items** (``{type: "function_call", call_id, ...}``):
         from the OpenAI Responses API loop's items list.

    Flat items are detected by having a ``type`` field without a ``role`` field
    (or ``type == "message"``). They are converted to canonical messages and
    grouped: consecutive assistant-role blocks merge into one message, and
    consecutive tool-role blocks merge into one message.

    Normalizes:
      - String content → ``[TextBlock]``
      - ``action: {…}`` → ``actions: [{…}]``
      - Preserves ``stop_reason`` on messages that have it
      - Strips ``acknowledged_safety_checks`` from ``computer_call_output``
    """
    result: list[CanonicalMessage] = []
    for msg in messages:
        if _is_flat_responses_item(msg):
            _ingest_flat_item(msg, result)
        else:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            blocks = _normalize_content(content, role)
            canonical: CanonicalMessage = {"role": role, "content": blocks}
            stop_reason = msg.get("stop_reason")
            if stop_reason:
                canonical["stop_reason"] = stop_reason
            result.append(canonical)
    return result


def _is_flat_responses_item(msg: dict[str, Any]) -> bool:
    """Check if a dict is a flat Responses API item (not a role-based message).

    Flat items have a ``type`` field and either no ``role`` field or
    ``type == "message"`` (Responses API wraps role-based content in a
    message item).
    """
    if "type" not in msg:
        return False
    msg_type = msg["type"]
    # {type: "message", role: ..., content: [...]} — Responses API message wrapper
    if msg_type == "message":
        return True
    # Has type but no role — flat item (function_call, computer_call, etc.)
    if "role" not in msg:
        return True
    return False


def _ingest_flat_item(
    item: dict[str, Any], result: list[CanonicalMessage]
) -> None:
    """Convert a flat Responses API item to canonical and append/merge into result.

    Groups consecutive same-role blocks into one message.
    """
    item_type = item.get("type", "")

    if item_type == "message":
        # {type: "message", role: "user"|"assistant", content: [{type: "input_text"|"output_text", ...}]}
        role = item.get("role", "user")
        content = item.get("content", [])
        blocks = _normalize_responses_content(content, role)
        if blocks:
            _append_or_merge(result, role, blocks)
        return

    if item_type == "function_call":
        block = FunctionCallBlock(
            type="function_call",
            id=item.get("call_id", item.get("id", "")),
            name=item.get("name", ""),
            arguments=item.get("arguments", ""),
        )
        _append_or_merge(result, "assistant", [block])
        return

    if item_type == "computer_call":
        block = ComputerCallBlock(
            type="computer_call",
            id=item.get("call_id", item.get("id", "")),
            actions=_normalize_actions(item),
        )
        _append_or_merge(result, "assistant", [block])
        return

    if item_type == "function_call_output":
        block = ToolResultBlock(
            type="tool_result",
            tool_use_id=item.get("call_id", ""),
            content=item.get("output", ""),
        )
        _append_or_merge(result, "tool", [block])
        return

    if item_type == "computer_call_output":
        # Strip acknowledged_safety_checks — not part of canonical format
        output = item.get("output", item.get("content", ""))
        if isinstance(output, dict):
            output = json.dumps(output)
        elif not isinstance(output, str):
            output = str(output)
        block = ToolResultBlock(
            type="tool_result",
            tool_use_id=item.get("call_id", ""),
            content=output,
        )
        _append_or_merge(result, "tool", [block])
        return

    if item_type == "reasoning":
        # OpenAI reasoning items — convert to ThinkingBlock
        summary = item.get("summary", [])
        text = ""
        if isinstance(summary, list):
            text = " ".join(
                s.get("text", "") for s in summary if isinstance(s, dict)
            )
        elif isinstance(summary, str):
            text = summary
        block = ThinkingBlock(type="thinking", thinking=text)
        _append_or_merge(result, "assistant", [block])
        return

    # Unknown flat item type — preserve as text in a user message
    text = json.dumps(item)[:500]
    block = TextBlock(type="text", text=f"[{item_type}: {text}]")
    _append_or_merge(result, "user", [block])


def _normalize_responses_content(
    content: Any, role: str
) -> list[ContentBlock]:
    """Normalize Responses API message content to canonical blocks.

    Handles Responses API content types: ``input_text``, ``output_text``,
    ``input_image``, ``computer_screenshot``, ``refusal``, ``summary_text``.
    """
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
        if btype in ("input_text", "output_text", "text", "summary_text"):
            blocks.append(TextBlock(type="text", text=block.get("text", "")))
        elif btype == "refusal":
            blocks.append(TextBlock(type="text", text=f"[refusal: {block.get('refusal', '')}]"))
        elif btype in ("input_image", "computer_screenshot"):
            # Image blocks in Responses API — preserve as text reference
            # (actual images are handled by the screenshot pipeline, not replayed)
            blocks.append(TextBlock(type="text", text=f"[{btype}]"))
        else:
            # Unknown Responses API content type — preserve as text
            blocks.append(TextBlock(type="text", text=block.get("text", str(block))))
    return blocks


def _append_or_merge(
    result: list[CanonicalMessage], role: str, blocks: list[ContentBlock]
) -> None:
    """Append blocks to the last message if same role, otherwise create new message."""
    if result and result[-1]["role"] == role:
        result[-1]["content"].extend(blocks)
    else:
        result.append(CanonicalMessage(role=role, content=list(blocks)))


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

        elif btype == "compaction_summary":
            blocks.append(CompactionSummaryBlock(
                type="compaction_summary",
                text=block.get("text", ""),
            ))

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
# Sanitize pipeline (US-OC-039)
# ---------------------------------------------------------------------------

SYNTHETIC_TOOL_RESULT_CONTENT = (
    "[compacted — result no longer available]"
)
"""Synthetic content for tool results inserted by repair_orphaned_pairs."""

_SKIP_SYNTHESIS_STOP_REASONS = frozenset({"error", "aborted"})
"""Stop reasons where we skip synthesizing tool results (the call was interrupted)."""


def repair_orphaned_pairs(
    messages: list[CanonicalMessage],
    *,
    synthesize: bool = True,
) -> list[CanonicalMessage]:
    """Repair orphaned tool call / result pairs in canonical messages.

    Consolidates logic from:
      - context.py:repair_tool_use_result_pairing (role-based, synthesis + stop_reason)
      - openai.py:_repair_orphaned_calls (flat items, drop computer_calls)

    Algorithm (3-pass, matching OpenClaw's session-transcript-repair.ts):
      1. Collect call IDs from assistant messages (FunctionCallBlock/ComputerCallBlock)
      2. Match ToolResultBlocks by tool_use_id. Drop orphaned results and duplicates.
      3. Insert synthetic ToolResultBlock for unmatched function_calls (skip if
         stop_reason is "error"/"aborted"). Drop unmatched computer_calls entirely
         (can't synthesize valid screenshots).

    Args:
        messages: Canonical messages to repair.
        synthesize: If True (default), insert synthetic error results for unmatched
            function_calls. If False, drop unmatched calls instead (replay mode).
    """
    if not messages:
        return []

    # --- Pass 1: Collect call IDs and result IDs ---
    call_ids: dict[str, tuple[str, str | None]] = {}  # id → (block_type, stop_reason)
    result_ids: set[str] = set()
    has_duplicate_results = False
    _result_id_counts: dict[str, int] = {}

    for msg in messages:
        role = msg.get("role", "")
        stop_reason = msg.get("stop_reason")
        for block in msg.get("content", []):
            btype = block.get("type", "")
            if btype in ("function_call", "computer_call"):
                bid = block.get("id", "")
                if bid:
                    call_ids[bid] = (btype, stop_reason)
            elif btype == "tool_result":
                rid = block.get("tool_use_id", "")
                if rid:
                    result_ids.add(rid)
                    _result_id_counts[rid] = _result_id_counts.get(rid, 0) + 1
                    if _result_id_counts[rid] > 1:
                        has_duplicate_results = True

    valid_call_id_set = set(call_ids.keys())
    matched_ids = valid_call_id_set & result_ids
    orphaned_calls = {
        cid: info for cid, info in call_ids.items() if cid not in matched_ids
    }
    orphaned_results = result_ids - valid_call_id_set

    # Fast path: nothing to repair
    if not orphaned_calls and not orphaned_results and not has_duplicate_results:
        return messages

    # --- Pass 2: Filter messages, dropping orphaned/duplicate results ---
    seen_result_ids: set[str] = set()
    filtered: list[CanonicalMessage] = []

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", [])

        if role == "tool":
            new_blocks: list[ContentBlock] = []
            for block in content:
                if block.get("type") == "tool_result":
                    rid = block.get("tool_use_id", "")
                    # Drop orphaned results (no matching call)
                    if rid and rid in orphaned_results:
                        continue
                    # Drop duplicates
                    if rid in seen_result_ids:
                        continue
                    if rid:
                        seen_result_ids.add(rid)
                new_blocks.append(block)
            if new_blocks:
                new_msg = dict(msg)
                new_msg["content"] = new_blocks
                filtered.append(new_msg)
        elif role == "assistant" and not synthesize:
            # Drop orphaned calls when not synthesizing (replay mode)
            new_blocks_a: list[ContentBlock] = []
            for block in content:
                btype = block.get("type", "")
                if btype in ("function_call", "computer_call"):
                    bid = block.get("id", "")
                    if bid in orphaned_calls:
                        continue
                new_blocks_a.append(block)
            if new_blocks_a:
                new_msg = dict(msg)
                new_msg["content"] = new_blocks_a
                filtered.append(new_msg)
        else:
            filtered.append(msg)

    if not synthesize:
        return filtered

    # --- Pass 3: Insert synthetic results for unmatched calls ---
    final: list[CanonicalMessage] = []
    for msg in filtered:
        final.append(msg)
        role = msg.get("role", "")
        if role != "assistant":
            continue

        stop_reason = msg.get("stop_reason")
        synthetic_blocks: list[ContentBlock] = []

        for block in msg.get("content", []):
            btype = block.get("type", "")
            if btype not in ("function_call", "computer_call"):
                continue
            bid = block.get("id", "")
            if bid not in orphaned_calls:
                continue

            call_type, _ = orphaned_calls[bid]

            if call_type == "computer_call":
                # Can't synthesize valid screenshots — drop by removing from
                # the assistant message content
                continue

            if stop_reason in _SKIP_SYNTHESIS_STOP_REASONS:
                continue

            synthetic_blocks.append(ToolResultBlock(
                type="tool_result",
                tool_use_id=bid,
                content=SYNTHETIC_TOOL_RESULT_CONTENT,
                is_error=True,
            ))

        if synthetic_blocks:
            final.append(CanonicalMessage(
                role="tool",
                content=synthetic_blocks,
            ))

    # Final cleanup: remove orphaned computer_calls from assistant messages
    cleaned: list[CanonicalMessage] = []
    for msg in final:
        role = msg.get("role", "")
        if role == "assistant":
            new_blocks_c: list[ContentBlock] = []
            for block in msg.get("content", []):
                btype = block.get("type", "")
                if btype == "computer_call":
                    bid = block.get("id", "")
                    if bid in orphaned_calls:
                        continue
                new_blocks_c.append(block)
            if new_blocks_c:
                new_msg = dict(msg)
                new_msg["content"] = new_blocks_c
                cleaned.append(new_msg)
        else:
            cleaned.append(msg)

    return cleaned


def ensure_valid_ordering(
    messages: list[CanonicalMessage],
) -> list[CanonicalMessage]:
    """Ensure messages don't end with role=assistant.

    Non-prefill models (like Opus 4.6) reject API calls where the last message
    is from the assistant. Appends a user continuation message if needed.
    """
    if not messages:
        return messages
    if messages[-1].get("role") == "assistant":
        messages = list(messages)
        messages.append(CanonicalMessage(
            role="user",
            content=[TextBlock(type="text", text="[Continue from where you left off.]")],
        ))
    return messages


def sanitize_items(
    messages: list[CanonicalMessage],
    target: Literal["openai-responses", "anthropic"],
) -> list[dict[str, Any]]:
    """Convert canonical messages to provider-specific format.

    Modeled on OpenClaw's sanitizeSessionHistory — linear pipeline of pure passes:
      1. repair_orphaned_pairs — fix split call/result pairs
      2. ensure_valid_ordering — no trailing assistant message
      3. Format conversion — canonical → target format

    Args:
        messages: Canonical messages (from normalize_to_canonical).
        target: Provider format to convert to.

    Returns:
        Provider-specific messages/items ready for the API.
    """
    messages = repair_orphaned_pairs(messages)
    messages = ensure_valid_ordering(messages)

    if target == "openai-responses":
        return canonical_to_responses_api(messages)
    elif target == "anthropic":
        return canonical_to_anthropic_messages(messages)
    else:
        raise ValueError(f"Unknown adapter target: {target}")


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
