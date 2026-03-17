# US-OC-013: Budget-Aware Compaction Split

## Context

The current `compact_messages()` uses a naive half-split — it divides messages into two equal token halves, summarizes the first, and keeps the second. This was fine when the agent started fresh after compaction, but with US-OC-012 (Transcript Replay), kept messages are now replayed as API history. If the kept half is too large relative to remaining context (after instructions + summary), it causes cascading compaction. OpenClaw solves this with `pruneHistoryForContextShare()` which calculates a budget for kept messages.

## OpenClaw Design Rationale

### What OpenClaw Does
- `pruneHistoryForContextShare()` calculates `budgetTokens = contextWindow * maxHistoryShare` (default 0.5)
- **Iteratively** drops oldest chunks from the to-keep portion until it fits within budget
- Calls `repairToolUseResultPairing()` **after each drop iteration** to fix orphaned tool_use/tool_result at the split boundary
- `splitPreservedRecentTurns()` splits out the last N user turns (+ their assistant/tool responses) **before** the budget loop — these are never pruned or summarized, guaranteeing the agent's most recent working state survives compaction
- `compaction-safeguard.ts` passes budget settings (instructions overhead, history share, recentTurnsPreserve) through the extension runtime
- **Duplicate detection**: tracks `seenToolResultIds` globally and drops results whose ID appears more than once — prevents accumulation across repair cycles
- **stopReason check**: skips synthetic result creation when `stopReason === "error"` or `"aborted"` — partial/malformed tool calls cause API 400 errors when paired with synthetic results

### What We Keep and Why
- **Budget-aware split calculation** — prevents cascading compaction when kept messages are replayed
- **Iterative chunk-drop loop** — matches OpenClaw's pattern; safely handles edge cases (oversized messages, uneven token distribution) without needing to pre-compute the exact split point
- **Recent turns preservation** — splits out the last N user turns before the budget loop begins. With cross-session runs reaching 500+ steps and multiple compaction cycles, the budget can get tight enough to aggressively trim even very recent messages. This guarantees the agent's in-flight working state (what it just did, what it's about to do) survives. Default: `recent_turns_preserve=3`
- **Tool pairing repair on kept messages** — API rejects orphaned tool_results; synthesis of missing results keeps pairs valid
- **Duplicate detection in repair** — repair runs multiple times (once per drop iteration); without dedup, synthetic results from earlier iterations accumulate
- **stopReason propagation** — `_extract_messages_for_compaction()` carries `stop_reason` from transcript entries into message dicts so repair can skip synthesis for error/aborted turns
- **max_history_share=0.5 default** — proven safe ratio from OpenClaw production

### What We Drop and Why
- **`stripToolResultDetails()`** — Clean drop, no future gap. CUA tool results are plain strings with no `details` field. The attack surface (untrusted Pi `toolResult.details`) doesn't exist in CUA.
- **Staged summarization (`summarizeInStages`)** — OpenClaw's map-reduce pattern: split messages into N chunks, summarize each independently, then merge partial summaries in stages until one remains. Needed for 1000+ message coding sessions where iterative rolling summarization produces an ever-growing accumulated summary that eats into the summarization prompt's budget. Dropped because compaction runs per-session (not cross-session), so each call sees at most ~100–200 messages where iterative rolling (US-OC-006) works fine. Future PRD item US-OC-016 if per-session message counts grow.
- **Pi SDK `session.compact()` / lane queueing / hooks** — Clean drop. CUA is single-process with no concurrency and no plugin ecosystem.

### Key Differences from OpenClaw
- `_extract_messages_for_compaction()` bridges the type gap: it converts `TranscriptEntry` objects into message dicts with `stop_reason` propagated, giving the repair function the metadata it needs without coupling to the session module

## Implementation Plan

### 1. Add `repair_tool_use_result_pairing()` to `context.py`

New dataclass + function:

```python
@dataclass
class ToolPairingRepairReport:
    messages: list[dict[str, Any]]
    dropped_orphan_count: int
    dropped_duplicate_count: int
    added_synthetic_count: int

def repair_tool_use_result_pairing(messages: list[dict[str, Any]]) -> ToolPairingRepairReport:
```

Algorithm (matching OpenClaw's `session-transcript-repair.ts`):
1. Iterate through messages. For each assistant message:
   a. Collect call IDs from function_call/computer_call blocks in content
   b. Scan forward for the next tool message(s), collecting result IDs
   c. Match results to calls by ID
2. **Orphaned tool_results** (result with no matching call in the preceding assistant message): drop → `dropped_orphan_count++`
3. **Orphaned calls** (call with no matching result): check `stop_reason` on the assistant message — if `"error"` or `"aborted"`, skip synthesis; otherwise insert synthetic error tool_result → `added_synthetic_count++`
4. **Duplicate results** (same `tool_use_id` seen before): drop → `dropped_duplicate_count++`
5. Synthetic content: `"[compaction] missing tool result — synthetic error result for transcript repair."`, `is_error: True`

### 2. Add `split_preserved_recent_turns()` to `context.py`

```python
def split_preserved_recent_turns(
    messages: list[dict[str, Any]], preserve_count: int = 3,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split out the last N user turns (+ their assistant/tool responses).
    Returns (pruneable_messages, preserved_messages).
    Preserved messages are never summarized or pruned."""
```

Algorithm:
1. Walk backward through messages, counting user messages
2. Once `preserve_count` user messages are found, split at that boundary
3. The preserved portion includes all messages from the Nth-from-last user message to the end
4. Repair tool pairing on the preserved portion (split could orphan pairs)

### 3. Update `compact_messages()` — budget-aware split with iterative pruning

New signature:
```python
async def compact_messages(
    messages, model, context_window, *,
    instructions_tokens: int = 0,
    max_history_share: float = 0.5,
    recent_turns_preserve: int = 3,
    custom_instructions: str | None = None,
) -> CompactionResult:
```

Replace the naive half-split with iterative budget-aware pruning:

```python
# 0. Split out preserved recent turns (never pruned)
pruneable, preserved = split_preserved_recent_turns(messages, recent_turns_preserve)

# 1. Budget calculation (budget is for kept pruneable messages only;
#    preserved messages are guaranteed to survive)
preserved_tokens = estimate_messages_tokens(preserved)
summary_estimate = SUMMARIZATION_OVERHEAD_TOKENS  # 4096
available_for_kept = (
    int(context_window * max_history_share)
    - instructions_tokens
    - summary_estimate
    - preserved_tokens
)
available_for_kept = max(available_for_kept, 2000)  # safety floor

# 2. Initial half-split on pruneable messages
halves = chunk_messages_by_token_share(pruneable, parts=2)
to_compact, to_keep = halves[0], halves[1] (or all if < 2)

# 3. Iterative pruning: while kept exceeds budget, split kept in half,
#    move older half to to_compact, repair pairing on remainder
while to_keep and estimate_messages_tokens(to_keep) > available_for_kept:
    sub_halves = chunk_messages_by_token_share(to_keep, parts=2)
    if len(sub_halves) < 2:
        break  # can't split further
    to_compact = to_compact + sub_halves[0]
    to_keep = sub_halves[1]
    repair = repair_tool_use_result_pairing(to_keep)
    to_keep = repair.messages

# 4. Final repair + recombine: to_keep + preserved = full kept portion
if to_keep:
    repair = repair_tool_use_result_pairing(to_keep)
    to_keep = repair.messages
final_kept = to_keep + preserved
first_kept_index = len(messages) - len(final_kept)

# ... summarize to_compact as before, return CompactionResult
```

### 4. Update `_extract_messages_for_compaction()` in `openclaw_agent.py`

Propagate `stop_reason` from transcript entries into message dicts:

```python
def _extract_messages_for_compaction(session_mgr) -> list[dict[str, Any]]:
    history = session_mgr.load_history()
    messages = []
    for entry in history:
        if entry.type != "message":
            continue
        msg_data = entry.data.get("message", {})
        msg = {
            "role": msg_data.get("role", "unknown"),
            "content": msg_data.get("content", ""),
        }
        stop_reason = msg_data.get("stop_reason")
        if stop_reason:
            msg["stop_reason"] = stop_reason
        messages.append(msg)
    return messages
```

### 5. Update `_compact_and_rebuild()` in `openclaw_agent.py`

Pass `instructions_tokens` through:
```python
compaction_result = await compact_messages(
    messages,
    self.model,
    overflow_cb.context_window,
    instructions_tokens=len(original_instructions) // 4,
)
```

### 6. Export new symbols via `openclaw/__init__.py`

Add: `repair_tool_use_result_pairing`, `ToolPairingRepairReport`, `split_preserved_recent_turns`

### 7. Tests (in `tests/test_openclaw_compaction.py`)

**Budget-aware split tests (~7):**
- `test_budget_adapts_when_instructions_large` — large instructions_tokens → fewer kept messages
- `test_budget_calculation_available_for_kept` — verify formula
- `test_kept_messages_within_budget` — after compaction, kept tokens ≤ available_for_kept
- `test_no_cascading_post_compaction` — kept + summary + instructions < context_window * 0.8
- `test_default_instructions_tokens_backward_compat` — no instructions_tokens arg works (defaults to 0)
- `test_custom_max_history_share` — verify custom share is respected
- `test_iterative_pruning_multiple_rounds` — budget so tight that kept portion needs multiple pruning rounds

**Recent turns preservation tests (~4):**
- `test_split_preserves_last_n_user_turns` — last 3 user turns (+ responses) in preserved
- `test_preserved_turns_survive_tight_budget` — even with tiny budget, preserved turns in final kept
- `test_preserved_turns_count_zero` — `recent_turns_preserve=0` → no preservation, all pruneable
- `test_preserved_turns_exceeds_messages` — fewer user turns than preserve_count → all preserved

**Tool pairing repair tests (~9):**
- `test_repair_drops_orphaned_tool_result` — result with no matching call → dropped
- `test_repair_synthesizes_missing_result` — call with no matching result → synthetic inserted
- `test_repair_skips_synthesis_for_error_stop_reason` — call with `stop_reason="error"` → no synthetic
- `test_repair_preserves_complete_pairs` — paired messages unchanged
- `test_repair_handles_computer_call` — computer_call orphan gets synthetic result
- `test_repair_drops_duplicates` — same tool_use_id appearing twice → second dropped
- `test_repair_empty_messages` — empty list → empty result
- `test_repair_at_split_boundary` — simulate split cutting between call and result
- `test_compact_applies_repair_to_kept` — end-to-end: compact_messages repairs kept portion

## Files Changed

| File | Change |
|------|--------|
| `submodules/cua/.../openclaw/context.py` | `ToolPairingRepairReport`, `repair_tool_use_result_pairing()`, `split_preserved_recent_turns()`, updated `compact_messages()` |
| `submodules/cua/.../openclaw_agent.py` | `_extract_messages_for_compaction()` propagates stop_reason; pass `instructions_tokens` to `compact_messages()` |
| `submodules/cua/.../openclaw/__init__.py` | Export new symbols |
| `tests/test_openclaw_compaction.py` | ~22 new tests |

## Verification

### Level 1
```bash
uv run ruff check submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/context.py tests/test_openclaw_compaction.py
uv run pytest tests/test_openclaw_compaction.py -v
```

### Level 2
```bash
CONTEXT_WINDOW_OVERRIDE=50000 bash run_magic_tower.sh 100
# Verify: compaction_count <= 2 (no cascading)
```
