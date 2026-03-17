# US-OC-006: Compaction Pipeline

## Context

Long-running agent tasks on remote Windows VMs accumulate messages and screenshots, eventually exceeding the context window. US-OC-005 detects when the context window is approaching overflow (80% threshold) and sets a `needs_compaction` flag. This story implements the actual compaction: summarizing older history while preserving key identifiers, so the agent can continue coherently.

## OpenClaw Design Rationale

### OpenClaw's Three Context Management Mechanisms

OpenClaw manages context pressure at three separate layers:

1. **Session Pruning** (transient, per-request) — Trims old tool results only, in-memory, driven by Anthropic cache TTL. Never modifies the JSONL transcript. Not relevant for CUA — we create a fresh agent per task run with no cache TTL to manage.

2. **`pruneHistoryForContextShare`** (pre-compaction budget guard) — Drops entire oldest message chunks when non-summarizable content exceeds `maxHistoryShare` (50% of context window). Then summarizes the dropped chunks. Safe to skip now because our stop-compact-resume creates a fresh agent; **critical to implement when kept message replay is added** (see Known Gaps).

3. **Compaction** (persistent, summarization) — Summarizes older history, persists summary to JSONL, keeps recent messages intact. This is what US-OC-006 implements.

We collapsed all three into one pipeline: `ContextOverflowCallback` (detection/truncation from US-OC-005) + `compact_messages()` (summarization).

### What OpenClaw Does
OpenClaw's compaction pipeline (`compaction.ts`) splits older messages into chunks by token budget, summarizes each chunk iteratively (feeding the previous summary as context), merges partial summaries, and injects the result as a compaction entry in the JSONL transcript. It uses adaptive chunk sizing (BASE_CHUNK_RATIO=0.4 → MIN_CHUNK_RATIO=0.15 for large messages), identifier preservation prompts, and a three-tier fallback (full → exclude oversized → static note). Orchestration (`compact.ts`) delegates to Pi SDK's `session.compact()` which can modify the live message array mid-conversation.

### What We Keep and Why
- **Chunk splitting by token share** — the core algorithm is model-agnostic and works with our `estimate_messages_tokens()`
- **Iterative summarization** — summarize chunk1, feed summary as context to chunk2; produces coherent rolling summaries
- **Fallback strategy** — full → exclude oversized → static note; handles edge cases gracefully
- **Identifier preservation prompt** — critical for CUA where file paths, game IDs, and coordinates must survive summarization
- **Adaptive chunk ratio** — large messages (screenshots, tool outputs) need smaller chunks
- **Constants** — BASE_CHUNK_RATIO=0.4, MIN_CHUNK_RATIO=0.15, SUMMARIZATION_OVERHEAD_TOKENS=4096

### What We Drop and Why

1. **`pruneHistoryForContextShare()`** — OpenClaw uses this as a budget guard: when kept messages + system prompt exceed `maxHistoryShare` (50%), it drops oldest chunks before summarizing. **Safe to skip now** because our stop-compact-resume creates a fresh agent with no carried-over message history. Note: OpenClaw does NOT split by half — the Pi SDK decides the split point internally, and the compaction-safeguard extension adjusts if budget is exceeded. Our half-split is a simplification. **Revisit when:** kept message replay is implemented — the new agent will carry the kept half as API message history, and budget-aware splitting becomes necessary (→ PRD Group A).

2. **`repairToolUseResultPairing()`** — `group_step_output()` (US-OC-004b) guarantees well-formed pairs at write time. **Safe to skip now** because the new agent starts fresh — kept messages are not replayed as raw API messages. **Revisit when:** kept message replay is implemented — `chunk_messages_by_token_share` can split between a tool_use and its result, and the API rejects orphaned tool_results (→ PRD Group A).

3. **`stripToolResultDetails()`** — Clean drop, no future gap. CUA tool results are plain strings with no `details` field. The attack surface (untrusted Pi `toolResult.details`) doesn't exist in CUA.

4. **Staged summarization (`summarizeInStages`)** — Dropped in favor of iterative rolling summarization. CUA runs are 50–100 steps (100–200 messages); OpenClaw needs multi-stage for 1000+ message coding sessions. **Revisit when:** task lengths grow to 500+ steps where accumulated summary context competes for space in the summarization prompt (→ PRD Group C).

5. **Pi SDK `session.compact()` / lane queueing / hooks** — Clean drop. CUA is single-process with no concurrency and no plugin ecosystem. Memory flush behavior is preserved via US-OC-005a + US-OC-008.

6. **Safety timeout wrapper** — Replaced by litellm's built-in timeout + 3-attempt exponential backoff retry. Minor gap: should add explicit `timeout=60` to `litellm.acompletion()` in `summarize_chunk()` — currently relying on litellm defaults which may be very long or unset (→ PRD Group B).

### Key Differences from OpenClaw

7. **Stop-compact-resume vs mid-conversation injection** — CUA's `ComputerAgent.run()` is an opaque async generator; we cannot inject compaction summaries mid-run. Instead: break out of the loop → compact transcript → create new agent with compaction context → resume. This is the highest-risk difference. **Key finding:** CUA's `on_llm_start` callback output is discarded after each turn — the agent loop always re-concatenates from immutable `old_items + new_items`. This means callback-based compaction cannot persist; stop-compact-resume is architecturally necessary, not just a convenience. A `custom_loop` parameter could enable OpenClaw-style mid-conversation compaction by managing a mutable message list (→ PRD Group D).

8. **Compaction summary in instruction (user message) vs system message** — Injected via `_create_compacted_instruction()` in `agent.run()`, not as a system message in conversation history. Multiple compactions stack summaries in instruction, bounded by `max_compactions=3`. Could compress (summarize-the-summaries) if stacked summaries grow too large. **Note for future:** may need to move to system message or conversation history entry when kept message replay is implemented.

9. **Transcript-based vs live message array** — We extract messages from `session_mgr.load_history()` rather than working with a live message array. Fidelity gap: CUA's `PromptInstructionsCallback` and `ImageRetentionCallback` modify messages before API calls; our transcript captures what CUA yields (output items), not what it sends (preprocessed messages). **Immaterial for summarization** — summaries don't need API-faithful messages. **Critical for future replay** — kept message replay would need the actual API-facing messages (→ PRD Group B).

10. **litellm.acompletion for summarization** — OpenClaw uses its own provider routing; we use litellm (already a dependency).

## Implementation Plan

### File: `agents/openclaw/context.py` (append below ContextOverflowCallback)

#### 1. Data structures

```python
@dataclass
class CompactionResult:
    summary: str                    # Compaction summary text
    tokens_before: int              # Estimated tokens before compaction
    tokens_after: int               # Estimated tokens after (summary + kept messages)
    first_kept_message_index: int   # Index where kept messages start
    chunks_processed: int           # Number of chunks summarized
```

#### 2. Constants

```python
BASE_CHUNK_RATIO = 0.4
MIN_CHUNK_RATIO = 0.15
SUMMARIZATION_OVERHEAD_TOKENS = 4096
DEFAULT_SUMMARY_FALLBACK = "No prior history."
```

#### 3. Prompt strings

```python
IDENTIFIER_PRESERVATION_INSTRUCTIONS = """Preserve all opaque identifiers exactly as written (no shortening or reconstruction), including UUIDs, hashes, IDs, tokens, API keys, hostnames, IPs, ports, URLs, and file names."""

MERGE_SUMMARIES_INSTRUCTIONS = """Merge these partial summaries into a single cohesive summary.

MUST PRESERVE:
- Active tasks and their current status (in-progress, blocked, pending)
- Batch operation progress (e.g., '5/17 items completed')
- The last thing the user requested and what was being done about it
- Decisions made and their rationale
- TODOs, open questions, and constraints
- Any commitments or follow-ups promised

PRIORITIZE recent context over older history."""
```

#### 4. Functions

| Function | Purpose |
|----------|---------|
| `chunk_messages_by_token_share(messages, parts=2)` | Split messages into N parts targeting equal token budgets |
| `chunk_messages_by_max_tokens(messages, max_tokens)` | Split messages respecting per-chunk token limit |
| `compute_adaptive_chunk_ratio(messages, context_window)` | Reduce chunk ratio for large avg message size |
| `serialize_messages_for_summary(messages)` | Convert message dicts to readable text, strip base64 |
| `async summarize_chunk(messages, model, previous_summary?, custom_instructions?)` | Summarize one chunk via litellm.acompletion |
| `async summarize_chunks_iterative(chunks, model, ...)` | Iterative: summarize chunk1, feed to chunk2, etc. |
| `async summarize_with_fallback(messages, model, context_window, ...)` | Three-tier: full → exclude oversized → static note |
| `async compact_messages(messages, model, context_window, custom_instructions?)` | **Main entry point** — orchestrates the full pipeline |

`compact_messages` flow:
1. Estimate total tokens via `estimate_messages_tokens()`
2. `compute_adaptive_chunk_ratio()` → chunk_ratio
3. max_chunk_tokens = `context_window * chunk_ratio - SUMMARIZATION_OVERHEAD_TOKENS`
4. Split messages into 2 halves by token share (first half = to-compact, second half = to-keep)
5. `summarize_with_fallback()` on the first half
6. Return `CompactionResult(summary, tokens_before, tokens_after, first_kept_index, chunks)`

### File: `agents/openclaw_agent.py` (modify perform_task)

#### Stop-Compact-Resume pattern

Wrap the agent run in a while-loop with max_compactions limit:

```python
max_compactions = 3
compaction_count = 0

while compaction_count <= max_compactions:
    async for result in agent.run(instruction):
        # ... existing step processing (unchanged) ...

        if overflow_cb.needs_compaction:
            # 1. Extract messages from transcript
            # 2. await compact_messages(...)
            # 3. session_mgr.append_compaction(...)
            # 4. instruction = _create_compacted_instruction(...)
            # 5. Recreate agent with same config
            # 6. Reset overflow_cb, increment compaction_count
            break  # restart while loop with new agent
    else:
        break  # agent.run() completed normally
```

Helper functions in the agent module:
- `_extract_messages_for_compaction(session_mgr)` — load transcript, convert to message dicts
- `_create_compacted_instruction(original, summaries)` — build instruction with "## Prior Context (Compacted)" prefix

### File: `agents/openclaw/__init__.py`

Export: `CompactionResult`, `compact_messages`

### File: `tests/test_openclaw_compaction.py` (new)

| Test class | Tests |
|------------|-------|
| `TestChunkSplitting` | split by token share (2 parts), empty messages, single message, max tokens limit, oversized message isolation, adaptive chunk ratio default/large |
| `TestSummarizeChunk` | produces string (mock litellm), identifier instructions in prompt, previous_summary passed, retry on failure |
| `TestCompactMessages` | returns CompactionResult, preserves kept messages, tokens_after < tokens_before, empty messages graceful |
| `TestIdentifierPreservation` | regex matches UUIDs, URLs, file paths |

## Verification

### Level 1
```bash
uv run ruff check submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/context.py tests/test_openclaw_compaction.py
uv run pytest tests/test_openclaw_compaction.py -v
```

### Level 2
```bash
CONTEXT_WINDOW_OVERRIDE=50000 bash run_magic_tower.sh 100
# Verify: compaction triggers, agent continues coherently, trajectory logs show compaction summary
```

## Known Gaps / Future PRD Items

Identified during design review (2026-03-13). No code changes needed for US-OC-006 — all gaps are future work.

### Group A: Kept Message Replay + Budget-Aware Compaction
Items 1–3 are tightly coupled — implement together.

1. **Budget-aware split** — Replace naive half-split with budget calculation: `available_for_kept = context_window - instructions_tokens - summary_estimate - margin`. Required when implementing kept message replay, since the kept half will compete for context space. (Points 1, 2)
2. **Tool_use/tool_result pairing repair** — `chunk_messages_by_token_share` can split between a tool_use and its result. Need pairing repair on kept messages before replaying them. API rejects orphaned tool_results. (Point 2)
3. **Kept message replay** — Feed the "kept" second half from compaction into the new agent's API message history (not just the summary in instruction). Makes post-compaction context richer. (Points 1, 2, 9)

### Group B: CUA Interaction Improvements
4. **Transcript fidelity** — Capture `on_llm_start` messages (the actual API payload after CUA callbacks process them) alongside the transcript. Gap becomes critical for kept message replay fidelity. (Point 9)
5. **Safety timeout** — Add explicit `timeout=60` to `litellm.acompletion()` in `summarize_chunk()`. (Point 6)

### Group C: Scalability
6. **Multi-stage summarization** — For 500+ step tasks, add split-summarize-merge pattern alongside iterative rolling summarization. (Point 4)

### Group D: Architecture Exploration
7. **Custom_loop exploration** — Investigate CUA's `custom_loop` parameter as a way to do in-memory compaction without stop-compact-resume. Could manage a mutable message list, enabling OpenClaw-style mid-conversation compaction. (Point 7)
