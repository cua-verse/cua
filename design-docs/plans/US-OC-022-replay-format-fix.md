# US-OC-022: Replay Message Format — Unnest to Responses API Items

## Problem

Transcript replay messages are silently mangled by CUA SDK agent loops because they use the wrong format.

**Current format** (Chat Completions — role-based with nested content blocks):
```json
{"role": "assistant", "content": [
  {"type": "text", "text": "I'll search memory"},
  {"type": "function_call", "id": "fc-1", "name": "memory_search", "arguments": "{}"}
]}
{"role": "tool", "content": [
  {"type": "tool_result", "tool_use_id": "fc-1", "content": "found it"}
]}
```

**Expected format** (Responses API — top-level type-dispatched items):
```json
{"type": "message", "role": "assistant", "content": [
  {"type": "output_text", "text": "I'll search memory"}
]}
{"type": "function_call", "call_id": "fc-1", "name": "memory_search", "arguments": "{}"}
{"type": "function_call_output", "call_id": "fc-1", "output": "found it"}
```

### Evidence

**Anthropic loop** (`loops/anthropic.py:169`):
```python
# Line 169 — assistant messages: joins text, DROPS all non-text blocks
content = "\n".join(item.get("text", "") for item in content)
```
All `function_call`, `computer_call` blocks in assistant content arrays are silently lost.

**Anthropic loop** — `role == "tool"` messages: not handled by any dispatch branch → silently dropped.

**Observed**: Claude Sonnet run `2026-03-16_claudesonnet4_020945_fbf0` has 148 replay messages after sanitization but only 2 messages in `turn_000/0000_api_start.json`. The GPT run `2026-03-16_gpt54_001727_4ac6` shows 8 messages because the OpenAI loop handles more formats.

## OpenClaw Design Rationale

### What CUA SDK Does

CUA SDK uses the OpenAI Responses API as its internal message format. All agent loops (`loops/anthropic.py`, `loops/openai.py`, etc.) receive messages as a list of Responses API items and convert them to their provider-specific format internally.

The Responses API item types the loops handle:
- `{"type": "message", "role": "user", "content": [...]}` — user messages
- `{"type": "message", "role": "assistant", "content": [...]}` — assistant messages
- `{"type": "function_call", "call_id": ..., "name": ..., "arguments": ...}` — tool calls
- `{"type": "function_call_output", "call_id": ..., "output": ...}` — tool results
- `{"type": "computer_call", "call_id": ..., "action": {...}}` — computer actions
- `{"type": "computer_call_output", "call_id": ..., "output": {...}}` — computer results

### What We Do Wrong

Our `build_replay_messages()` produces Chat Completions format (role-based messages with content arrays). Our `convert_to_responses_api_format()` only remaps content block type names (`text` → `input_text`/`output_text`) but **doesn't unnest** structured blocks into top-level items.

### The Fix

Rewrite `convert_to_responses_api_format()` to:
1. Remove the `_is_openai_model()` guard — ALL backends need this
2. **Unnest** assistant content blocks into separate top-level items
3. **Convert** tool role messages into `function_call_output` items
4. Keep user messages as `{"type": "message", "role": "user", ...}` wrappers

## Implementation Plan

### File: `agents/openclaw/session.py`

Replace `convert_to_responses_api_format()` and remove `_is_openai_model()`:

```python
def convert_to_responses_api_items(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert replay messages from Chat Completions format to Responses API items.

    CUA SDK agent loops dispatch on top-level `type` field, not `role`.
    This function unnests content blocks into separate top-level items.
    """
    items: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "user":
            # User messages stay as message wrappers
            if isinstance(content, str):
                items.append({
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": content}],
                })
            elif isinstance(content, list):
                converted_blocks = []
                for block in content:
                    btype = block.get("type", "")
                    if btype == "text":
                        converted_blocks.append({"type": "input_text", "text": block.get("text", "")})
                    elif btype in ("image", "image_url"):
                        converted_blocks.append(block)  # pass through
                    elif btype == "tool_result":
                        # Orphaned tool result in user message — shouldn't happen after sanitize,
                        # but convert defensively
                        items.append({
                            "type": "function_call_output",
                            "call_id": block.get("tool_use_id", ""),
                            "output": block.get("content", ""),
                        })
                        continue  # don't add to converted_blocks
                    else:
                        converted_blocks.append(block)
                if converted_blocks:
                    items.append({
                        "type": "message",
                        "role": "user",
                        "content": converted_blocks,
                    })
            continue

        if role == "assistant":
            # Unnest: text blocks → message wrapper, structured blocks → top-level items
            text_blocks = []
            for block in (content if isinstance(content, list) else [{"type": "text", "text": content}]):
                btype = block.get("type", "")
                if btype == "text":
                    text_blocks.append({"type": "output_text", "text": block.get("text", "")})
                elif btype == "function_call":
                    # Flush text blocks first
                    if text_blocks:
                        items.append({"type": "message", "role": "assistant", "content": text_blocks})
                        text_blocks = []
                    items.append({
                        "type": "function_call",
                        "call_id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "arguments": block.get("arguments", ""),
                    })
                elif btype == "computer_call":
                    if text_blocks:
                        items.append({"type": "message", "role": "assistant", "content": text_blocks})
                        text_blocks = []
                    items.append({
                        "type": "computer_call",
                        "call_id": block.get("id", ""),
                        "action": block.get("action", {}),
                    })
                else:
                    text_blocks.append(block)
            if text_blocks:
                items.append({"type": "message", "role": "assistant", "content": text_blocks})
            continue

        if role == "tool":
            # Each tool_result block becomes a top-level function_call_output
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "tool_result":
                        items.append({
                            "type": "function_call_output",
                            "call_id": block.get("tool_use_id", ""),
                            "output": block.get("content", ""),
                        })
                    elif block.get("type") == "computer_call_output":
                        items.append({
                            "type": "computer_call_output",
                            "call_id": block.get("call_id", ""),
                            "output": block.get("output", {}),
                        })
            elif isinstance(content, str):
                items.append({
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": content}],
                })
            continue

        # Fallback: wrap as user message
        items.append({
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": str(content)}],
        })

    return items
```

### File: `agents/openclaw_agent.py`

Update import and call site:
- Replace `convert_to_responses_api_format` import with `convert_to_responses_api_items`
- Change call from `convert_to_responses_api_format(replay_messages, self.model)` to `convert_to_responses_api_items(replay_messages)` (no model arg needed)

### File: `agents/openclaw/__init__.py`

Update export: `convert_to_responses_api_items` replacing `convert_to_responses_api_format`

### Tests

Add to `tests/test_openclaw_replay.py`:
- Assistant message with mixed text + function_call blocks → produces message + top-level function_call
- Tool message with tool_result blocks → produces function_call_output items
- User message with text → produces message wrapper with input_text
- Round-trip: the output items should be consumable by `_convert_responses_items_to_completion_messages` in the Anthropic loop

## Acceptance Criteria

- Level 1: Lint passes
- Level 1: Unit tests for all conversion cases
- Level 1: All models get converted (no `_is_openai_model` guard)
- Level 2: Anthropic run with prior transcript shows replay messages in api_start.json
- Level 2: OpenAI run with prior transcript has no API errors
