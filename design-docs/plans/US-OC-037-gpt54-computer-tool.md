# US-OC-037: CUA OpenAI Loop — GPT 5.4 `computer` Tool Support

## Context

The CUA framework's OpenAI loop (`loops/openai.py`) only supports the `computer-use-preview` model. GPT 5.4 introduces a new computer use API with key differences: a simpler `{"type": "computer"}` tool schema (no display dimensions), batched `actions` arrays instead of single `action` objects, a different screenshot output format (`computer_screenshot` vs `input_image`), no `acknowledged_safety_checks`, and content blocks typed as `input_text`/`output_text` instead of bare `text`. This story adds GPT 5.4 support following the same pattern as US-OC-023 (Anthropic Opus 4.6 tool version compatibility).

## OpenClaw Design Rationale

Not applicable — this is a CUA SDK extension, not an OpenClaw reproduction. The Anthropic loop's `MODEL_TOOL_MAPPING` pattern is the design precedent. The orphan call repair follows OpenClaw's `session-transcript-repair.ts` pattern.

## Implementation

### 1. Extend OpenAI loop registration (`loops/openai.py`)

- Updated `@register_agent` regex to `r".*(^|/)(computer-use-preview|gpt-5\.4)"` to match both model patterns
- Added `_is_gpt54(model)` helper — checks for `gpt-5.4` in model name
- Updated `_map_computer_tool_to_openai(computer_handler, model)` — GPT 5.4 returns `{"type": "computer"}` (no dimensions/environment), computer-use-preview keeps `{"type": "computer_use_preview", display_width, display_height, environment}`
- Updated `_prepare_tools_for_openai(tool_schemas, model)` — passes model through for tool mapping
- Updated `predict_click()` — uses correct tool schema per model, handles both `action` (singular) and `actions` (array) when extracting click coordinates
- Added `get_screenshot_output_type(model)` — returns `"computer_screenshot"` for GPT 5.4, `"input_image"` for preview
- `predict_step()` sets `self.screenshot_output_type` on each call and runs `_normalize_messages_for_gpt54()` on input messages

### 2. Completion-to-Responses format normalization (`loops/openai.py`)

**The biggest piece of work.** After compaction, kept messages are in Anthropic completion format (`{role, content: [blocks]}`) with content block types that GPT 5.4's Responses API rejects. `_normalize_messages_for_gpt54()` handles:

1. **`acknowledged_safety_checks`** — stripped from `computer_call_output` items (not supported by GPT 5.4's `computer` tool; appears in replayed history from prior `computer-use-preview` sessions)
2. **`type: "text"` content blocks** — converted to `input_text` (user messages) or `output_text` (assistant messages)
3. **`computer_call` content blocks** — extracted from content arrays and emitted as top-level `computer_call` items with `call_id`. Stale entries with empty `action: {}` (from old transcripts before the `actions` fix) are serialized as text instead of producing invalid API items
4. **`function_call` content blocks** — extracted as top-level `function_call` items
5. **`tool_result` content blocks** — converted to `function_call_output` items with proper `call_id` mapping
6. **String `content` fields** — wrapped as `[{type: input_text/output_text, text: ...}]`
7. **Already-valid Responses API types** — passed through unchanged

### 3. Support batched actions in `agent.py` `_handle_item()`

- Checks for `actions` (array, GPT 5.4) first, falls back to `action` (singular, computer-use-preview)
- Executes each action sequentially via `getattr(computer, action_type)(**action_args)`
- Takes screenshot only after the **last** action (not each one)
- Returns one `computer_call_output` per `computer_call` (API requires 1:1 call_id matching)
- Breaks out of loop early if a `terminate` action is encountered

### 4. Model-aware screenshot output format (`agent.py`)

- `_screenshot_output_type` attribute set on `ComputerAgent.__init__()` from `agent_loop.screenshot_output_type`
- GPT 5.4: `{"type": "computer_screenshot", "image_url": "data:...", "detail": "original"}`
- computer-use-preview: `{"type": "input_image", "image_url": "data:..."}`
- `acknowledged_safety_checks` conditionally included only for non-GPT-5.4 models

### 5. Orphan call repair in compaction (`agent_loop.py`)

- Added `repair_tool_use_result_pairing()` call on kept messages in `_build_compacted_items()`
- When compaction splits a function_call/tool_result pair across the compact/kept boundary, the kept portion has an orphaned function_call. The Anthropic loop handles this in its replay conversion, but the OpenAI loop sends items directly — orphans cause API rejection
- Fix matches OpenClaw's `session-transcript-repair.ts` pattern: repair at the source (during compaction), not at the consumer

### 6. Transcript format updates (`transcript.py`)

- `group_step_output()` now stores `actions` (plural) when present in `computer_call` items, falls back to `action` (singular) — preserves the batched format through the transcript round-trip
- `computer_call_output` handling recognizes `computer_screenshot` type alongside `input_image` for screenshot path resolution

### 7. Logging and serialization updates

- **`tools.py`**: Added `_get_action_type_label(item)` helper — returns `"click+keypress+keypress"` for batched actions, single type for singular. Used in `on_computer_call_start` and `on_computer_call_end`
- **`memory_flush.py`**: Updated `_serialize_content_blocks()` to iterate `actions` array, serializing each action individually
- **`context.py`**: `serialize_messages_for_summary()` checks `block.get("action") or block.get("actions", {})` for computer_call blocks

## Files Modified

| File | Change |
|------|--------|
| `submodules/cua/libs/python/agent/agent/loops/openai.py` | Model regex, tool mapping, `_normalize_messages_for_gpt54()`, `get_screenshot_output_type()`, `predict_click()` update |
| `submodules/cua/libs/python/agent/agent/agent.py` | Batched actions in `_handle_item()`, `_screenshot_output_type` flag, conditional `acknowledged_safety_checks` |
| `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/agent_loop.py` | `repair_tool_use_result_pairing()` in `_build_compacted_items()` |
| `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/transcript.py` | `actions` plural storage, `computer_screenshot` recognition |
| `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/tools.py` | `_get_action_type_label()` for batched action logging |
| `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/memory_flush.py` | Batched actions serialization |
| `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/context.py` | `serialize_messages_for_summary()` handles `actions` key |

## Issues Encountered During Implementation

1. **`acknowledged_safety_checks` rejected** — GPT 5.4's `computer` tool doesn't support this field. Fixed by stripping from both new outputs and replayed history.
2. **`type: "text"` content blocks rejected** — Compaction summaries from Anthropic model use `type: "text"`, but Responses API requires `input_text`/`output_text`. Required full content block type conversion.
3. **`computer_call` content blocks rejected** — After compaction, kept messages contain Anthropic completion format with `computer_call` blocks embedded in content arrays. Required expanding to top-level Responses API items.
4. **Empty `action: {}` in stale transcripts** — Prior runs stored `action: item.get("action", {})` which is `{}` for GPT 5.4 (which uses `actions` plural). Fixed by validating action has a `type` before emitting.
5. **Orphaned function calls after compaction** — Compaction can split a function_call/tool_result pair. The Anthropic loop repairs this during replay conversion, but the OpenAI loop sends items directly. Fixed by adding `repair_tool_use_result_pairing()` in `_build_compacted_items()`.

## Verification Results

1. `uv run ruff check .` — passes
2. `bash run_magic_tower.sh 15 openai/gpt-5.4` — 15 steps completed (MAX_STEPS_EXCEEDED)
3. `CONTEXT_WINDOW_OVERRIDE=50000 bash run_magic_tower.sh 50 openai/gpt-5.4` — 50 steps completed, no compaction needed (79% usage)
4. `CONTEXT_WINDOW_OVERRIDE=25000 bash run_magic_tower.sh 50 openai/gpt-5.4` — compaction fired (7437→6108 tokens), agent continued post-compaction, completed with `FailureMode.NONE` (exit code 0), evaluation scored 0.33
