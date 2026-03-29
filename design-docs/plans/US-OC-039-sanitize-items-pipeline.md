# US-OC-039: Centralized Format Adapter — sanitize_items() Pipeline

## Context

US-OC-038 created `canonical.py` with typed messages (`CanonicalMessage`) and two format converters (`canonical_to_responses_api`, `canonical_to_anthropic_messages`). However, the OpenAI loop still has its own 130-line `_normalize_messages_for_gpt54()` and 77-line `_repair_orphaned_calls()`. Three repair functions exist across the codebase doing similar work on different data shapes. This story unifies them into a single `sanitize_items()` pipeline.

**Cross-package constraint**: `openai.py` (in `agent/loops/`) cannot import from `cua_bench/agents/openclaw/`. Therefore, sanitization must be called from `agent_loop.py` (in `openclaw/`), not from `openai.py`.

## OpenClaw Design Rationale

**What OpenClaw Does**: `sanitizeSessionHistory()` in `pi-embedded-runner/google.ts` — a linear pipeline of 7+ pure passes on `AgentMessage[]`. A single `repairToolUseResultPairing()` handles all providers. `TranscriptPolicy` flags control which passes run.

**What We Keep**: Pipeline-of-passes pattern. Single repair function on canonical messages. Format conversion as the final step.

**What We Drop**: TranscriptPolicy (US-OC-041), thinking sanitization (US-OC-041), image sanitization (session.py already does this), inter-session annotation (not applicable).

**Key Difference from OpenClaw**: Our canonical format uses role-based messages (like OpenClaw's AgentMessage), but we also need to ingest flat Responses API items from the current run. OpenClaw doesn't have this dual-format challenge because it controls the entire pipeline.

## Implementation Plan

### Step 1: Extend `normalize_to_canonical()` to handle flat Responses API items

**File**: `canonical.py`

Currently `normalize_to_canonical()` only handles role-based messages (`{role, content}`). The items going through the run() loop are flat Responses API items (`{type: "function_call", call_id, ...}`, `{type: "computer_call_output", ...}`, `{type: "message", role, content: [...]}`).

Add flat-item detection: if a dict has `type` but no `role` (or `type` == "message"), convert it to canonical. Map:
- `{type: "message", role, content}` → `CanonicalMessage(role, content)`
- `{type: "function_call", call_id, name, arguments}` → assistant message with FunctionCallBlock (using `call_id` → `id`)
- `{type: "function_call_output", call_id, output}` → tool message with ToolResultBlock
- `{type: "computer_call", call_id, ...}` → assistant message with ComputerCallBlock
- `{type: "computer_call_output", call_id, ...}` → tool message with ToolResultBlock
- `{type: "reasoning", ...}` → assistant message with ThinkingBlock (preserve for now, US-OC-041 will sanitize)
- Strip `acknowledged_safety_checks` from `computer_call_output` during ingestion

Group consecutive assistant-role blocks into one message. Group consecutive tool-role blocks into one message.

### Step 2: Add `repair_orphaned_pairs()` to `canonical.py`

**File**: `canonical.py`

New function operating on `list[CanonicalMessage]`:

```python
def repair_orphaned_pairs(
    messages: list[CanonicalMessage],
    *, synthesize: bool = True,
) -> list[CanonicalMessage]:
```

Consolidates logic from:
- `context.py:repair_tool_use_result_pairing()` — synthesis + stop_reason handling
- `openai.py:_repair_orphaned_calls()` — drop orphaned computer_calls, synthesize function_call_output

Algorithm (3-pass, matching OpenClaw):
1. Collect call IDs from assistant messages (FunctionCallBlock/ComputerCallBlock)
2. Match ToolResultBlocks by `tool_use_id`. Drop orphaned results and duplicates.
3. Insert synthetic ToolResultBlock for unmatched function_calls (skip if stop_reason is "error"/"aborted"). Drop unmatched computer_calls entirely (can't synthesize valid screenshots).

**`synthesize=False`** flag: for session replay where we just want to drop (matches `_repair_orphaned_tool_pairs` behavior). Default `True` matches the compaction pipeline behavior.

### Step 3: Add `ensure_valid_ordering()` to `canonical.py`

**File**: `canonical.py`

Ensures messages don't end with `role=assistant` (non-prefill models reject this). Appends a `[Continue from where you left off.]` user message if needed. This logic currently lives in `_build_compacted_items()` — extract it into a reusable pass.

### Step 4: Add `sanitize_items()` to `canonical.py`

**File**: `canonical.py`

```python
def sanitize_items(
    messages: list[CanonicalMessage],
    target: Literal["openai-responses", "anthropic"],
) -> list[dict[str, Any]]:
    """Convert canonical messages to provider-specific format.

    Pipeline:
      1. repair_orphaned_pairs()
      2. ensure_valid_ordering()
      3. Format conversion (canonical_to_responses_api or canonical_to_anthropic_messages)
    """
```

### Step 5: Wire into `agent_loop.py`

**File**: `agent_loop.py`

In the `run()` method, before `predict_step()` (around line 168), add sanitization:

```python
# Determine adapter target from model name
target = _get_adapter_target(self.model)

# Sanitize: canonical repair + format conversion
from .canonical import normalize_to_canonical, sanitize_items
canonical = normalize_to_canonical(preprocessed)
preprocessed = sanitize_items(canonical, target=target)
```

Add helper:
```python
def _get_adapter_target(model: str) -> str:
    """Return adapter target based on model name."""
    if re.search(r"gpt-|computer-use-preview|openai/", model, re.IGNORECASE):
        return "openai-responses"
    return "anthropic"
```

**Update `_compact_in_place()`** (line 413-416): Replace the bridge with sanitize_items:
```python
canonical_messages = self._build_compacted_items(...)
target = _get_adapter_target(self.model)
compacted_items = sanitize_items(canonical_messages, target=target)
```

**Update `_build_compacted_items()`**: Remove the trailing-assistant check (lines 480-488) — it's now in `ensure_valid_ordering()`.

### Step 6: Delete from `openai.py`

**File**: `openai.py`

- Delete `_normalize_messages_for_gpt54()` (lines 70-221)
- Delete `_repair_orphaned_calls()` (lines 225-304)
- Remove lines 370-374 in `predict_step()`:
  ```python
  if _is_gpt54(model):
      messages = _normalize_messages_for_gpt54(messages)
      messages = _repair_orphaned_calls(messages)
  ```

Keep `_is_gpt54()` and `get_screenshot_output_type()` — they're used by other parts of the loop. US-OC-040 will remove them.

### Step 7: Update `__init__.py` exports

**File**: `__init__.py`

Add exports: `sanitize_items`, `repair_orphaned_pairs`, `ensure_valid_ordering`.

### Step 8: Unit tests

**File**: `tests/test_sanitize_items.py` (new)

Test groups:
1. **`normalize_to_canonical()` with flat items**: function_call, function_call_output, computer_call, computer_call_output, message (user/assistant), reasoning items, mixed flat + role-based
2. **`repair_orphaned_pairs()`**: orphaned function_call → synthetic result, orphaned computer_call → dropped, orphaned tool_result → dropped, paired items unchanged, stop_reason="error" → skip synthesis, synthesize=False → drop-only mode
3. **`sanitize_items(target='openai-responses')`**: all 7+ content block types produce valid Responses API items
4. **`sanitize_items(target='anthropic')`**: all content block types produce valid Anthropic messages
5. **Integration**: role-based messages with tool pairs → sanitize_items → valid output with correct pairing

### Step 9: Update existing tests

Update any tests that reference `_normalize_messages_for_gpt54` or `_repair_orphaned_calls` from openai.py to use the new pipeline.

## Files Changed

| File | Change |
|------|--------|
| `openclaw/canonical.py` | Add flat-item ingestion, repair_orphaned_pairs, ensure_valid_ordering, sanitize_items |
| `openclaw/agent_loop.py` | Wire sanitize_items before predict_step, update _compact_in_place bridge, add _get_adapter_target |
| `openclaw/__init__.py` | Export new functions |
| `agent/loops/openai.py` | Delete _normalize_messages_for_gpt54, _repair_orphaned_calls, remove call site |
| `tests/test_sanitize_items.py` | New comprehensive test file |
| Existing test files | Update references to deleted functions |

## Verification

1. `uv run ruff check submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/ tests/`
2. `uv run pytest tests/test_sanitize_items.py -v`
3. `uv run pytest tests/ -v` (full test suite — no regressions)
4. `bash run_magic_tower.sh 15 openai/gpt-5.4` (Level 2)
5. `bash run_magic_tower.sh 15 openai/computer-use-preview` (Level 2)
6. `CONTEXT_WINDOW_OVERRIDE=25000 bash run_magic_tower.sh 50 openai/gpt-5.4` (Level 2 — compaction + post-compaction)

## Risks

- **Flat-item round-trip fidelity**: Flat Responses API items round-tripping through canonical must not lose information. Key concern: `reasoning` items from GPT 5.4 — need to pass through as ThinkingBlocks. Mitigated by thorough unit tests.
- **computer-use-preview regression**: Currently normalization only runs for GPT 5.4. Now it runs for all OpenAI models. Safe because canonical_to_responses_api handles all item types correctly, but needs VM regression testing.
- **Compaction pipeline**: repair_tool_use_result_pairing() in context.py stays unchanged — it operates on role-based messages internally within compact_messages(). Only the post-compaction bridge in _compact_in_place changes.
