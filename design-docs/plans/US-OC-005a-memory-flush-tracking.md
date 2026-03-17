# US-OC-005a: Memory Flush Tracking + Triggering

## What Was Implemented

### Part 1 — Schema & Tracking (`session.py`)

| Component | Location |
|-----------|----------|
| `SessionState.memory_flush_at: str \| None` | Field — ISO timestamp of last flush |
| `SessionState.memory_flush_compaction_count: int \| None` | Field — compaction count at flush time |
| `to_dict()` / `from_dict()` | Conditional serialization (omit when None) |
| `SessionManager.record_memory_flush()` | Sets both fields to now/current compaction count, persists |
| `has_already_flushed_for_current_compaction(state)` | Guard: returns True when flush count == compaction count |

### Part 2 — Trigger Logic & Constants (`session.py`)

| Component | Location |
|-----------|----------|
| `SILENT_REPLY_TOKEN = "[!silent]"` | Constant — model replies with this when nothing to persist |
| `MEMORY_FLUSH_PROMPT` | Constant — user-turn prompt instructing memory persistence |
| `MEMORY_FLUSH_SYSTEM_PROMPT` | Constant — system prompt for flush turn |
| `DEFAULT_MEMORY_FLUSH_SOFT_THRESHOLD_TOKENS = 4000` | Constant — soft token buffer before compaction |
| `should_run_memory_flush(state, current_tokens, context_window, ...)` | Trigger: fires when tokens >= (context_window - reserve - soft_threshold) AND not already flushed |

### Part 3 — Flush Execution (`openclaw_agent.py`)

| Component | Description |
|-----------|-------------|
| `OpenClawAgent._run_memory_flush()` | Single litellm turn with flush prompts + `memory_write` tool schema. Handles tool calls (writes to MemoryStore), silent replies, and text-only replies. Logs both user and assistant messages to transcript. Non-fatal on failure (records flush anyway to prevent retry loops). |
| Agent loop wiring (line 334–346) | Before `_compact_and_rebuild()`: checks `should_run_memory_flush()` with overflow callback's `current_tokens` / `context_window`, runs flush if needed |

### Part 4 — Exports (`__init__.py`)

All new symbols exported: `MEMORY_FLUSH_PROMPT`, `MEMORY_FLUSH_SYSTEM_PROMPT`, `SILENT_REPLY_TOKEN`, `should_run_memory_flush`, `has_already_flushed_for_current_compaction`.

### Part 5 — Tests (`test_openclaw_session.py`)

79 total tests (19 new):
- `TestSessionState`: 3 new (defaults, round-trip, omission-when-None)
- `TestRecordMemoryFlush`: 2 (basic, tracks compaction count)
- `TestHasAlreadyFlushedForCurrentCompaction`: 3 (None, match, differ)
- `TestShouldRunMemoryFlush`: 7 (above/below threshold, already flushed, after new compaction, zero tokens, custom threshold, default value)
- `TestMemoryFlushConstants`: 4 (token value, prompt content checks)

---

## Differences from OpenClaw

### Tracking fields

| Aspect | OpenClaw | Ours |
|--------|----------|------|
| `memoryFlushAt` type | `number` (epoch ms) | `str \| None` (ISO 8601) — matches our `created_at`/`updated_at` pattern |
| `memoryFlushCompactionCount` type | `number` | `int \| None` — identical semantics |
| Storage location | `SessionEntry` in sessions JSON store | `SessionState` dataclass in `state.json` |
| Update mechanism | Inline `updateSessionStoreEntry()` with async callback | `SessionManager.record_memory_flush()` method |
| Reset on new session | Explicit reset to `undefined` on `/new`/`/reset` (`session.ts:525-526`) | Implicit — fields default to `None` on new `SessionState`, but `init_session()` loads existing state, preserving flush fields across runs. Acceptable because `compaction_count` is also cumulative, so the guard naturally allows a new flush after the next compaction. |

### Guard function

| Aspect | OpenClaw (`hasAlreadyFlushedForCurrentCompaction`) | Ours (`has_already_flushed_for_current_compaction`) |
|--------|----------|------|
| Signature | `entry: Pick<SessionEntry, "compactionCount" \| "memoryFlushCompactionCount">` | `state: SessionState` |
| Null handling | `compactionCount ?? 0`, then type-checks `memoryFlushCompactionCount` | Checks `is None`, returns False early |
| Logic | `typeof lastFlushAt === "number" && lastFlushAt === compactionCount` | `state.memory_flush_compaction_count == state.compaction_count` |

Functionally identical.

### Trigger function

| Aspect | OpenClaw (`shouldRunMemoryFlush`) | Ours (`should_run_memory_flush`) |
|--------|----------|------|
| Token source | `resolveFreshSessionTotalTokens(entry)` or `tokenCount` override — resolves from session entry's stale/fresh token tracking | `current_tokens` parameter — directly from `ContextOverflowCallback.current_tokens` (always fresh, estimated pre-LLM) |
| Token projection | `resolveEffectivePromptTokens(base + lastOutput + promptEstimate)` — projects next input size from transcript tail | Not needed — `overflow_cb.current_tokens` is already the estimated next-input size |
| Threshold formula | `contextWindow - reserveTokensFloor - softThresholdTokens` | `context_window - reserve_tokens - soft_threshold_tokens` — identical |
| `reserveTokensFloor` | Resolved from `cfg.agents.defaults.compaction.reserveTokensFloor` (default `DEFAULT_PI_COMPACTION_RESERVE_TOKENS_FLOOR = 20000`) | Parameter with `default=DEFAULT_MEMORY_FLUSH_RESERVE_TOKENS_FLOOR` (20000) — matches OpenClaw's hardcoded default |
| `softThresholdTokens` | Resolved from config (default 4000) | Parameter with `default=4000` |
| Input validation | `Math.max(1, Math.floor(...))` on all numeric inputs | Simpler: `max(0, ...)` on threshold, `<= 0` checks on tokens |

Functionally equivalent for our use case. OpenClaw's complexity around stale/fresh token tracking and transcript reading is because it runs asynchronously between turns (tokens may be out of date); our overflow callback always has a fresh estimate.

### Flush execution

| Aspect | OpenClaw (`runMemoryFlushIfNeeded`) | Ours (`_run_memory_flush`) |
|--------|----------|------|
| Conversation context | Full session file loaded via `SessionManager.open()` — flush agent sees entire conversation history | Full transcript via `_extract_flush_context()` — text-only user/assistant messages from the complete history (US-OC-025). Can't reuse compaction's extractor because raw content blocks (tool calls, computer calls, tool role) cause litellm API errors. |
| Execution model | `runEmbeddedPiAgent()` — full embedded agent run with model fallback, sandbox config, provider routing, auth profiles | Single `litellm.acompletion()` call with `memory_write` tool |
| Tool access | Full agent tool set (filesystem, shell, etc.) — memory written via filesystem (`memory/YYYY-MM-DD.md`) | Single `memory_write` tool — memory written via `MemoryStore` API |
| Tool call rounds | Full agent loop (can do multiple tool calls) | Single round — one LLM call, execute any tool calls, done |
| Model fallback | `runWithModelFallback()` — tries alternative models on failure | None — uses `self.model` only |
| Transcript logging | Handled by embedded agent run infrastructure | Manual: appends user + assistant messages to session transcript |
| Compaction during flush | Possible — `onAgentEvent` detects compaction within the flush run and increments `compactionCount` | Not possible — single LLM call, no compaction |
| Prompt | `resolveMemoryFlushPromptForRun()` — substitutes `YYYY-MM-DD` with timezone-aware date stamp, appends `Current time:` line | Static `MEMORY_FLUSH_PROMPT` — references `memory_write` tool directly |
| System prompt | `extraSystemPrompt + memoryFlushSettings.systemPrompt` | Static `MEMORY_FLUSH_SYSTEM_PROMPT` |
| Failure handling | `logVerbose`, returns session entry unchanged | Prints warning, records flush anyway to prevent retry loops |
| Silent reply | Model replies with `[!silent]` (from `SILENT_REPLY_TOKEN`) | Same — checks for `[!silent]` in reply |
| `enabled` config | `resolveMemoryFlushSettings()` returns null if `enabled: false` | Always enabled when compaction triggers — no config toggle |

### Trigger wiring

| Aspect | OpenClaw | Ours |
|--------|----------|------|
| Where | `runMemoryFlushIfNeeded()` called from `runReplyAgent()` *before* each agent turn — evaluated pre-emptively on every turn | Per-step check inside `async for` step loop (US-OC-025), right before compaction check — evaluated every step where `overflow_cb.current_tokens` is fresh from `on_llm_start`. OpenClaw checks pre-turn with transcript-based token estimates; we check per-step because CUA's `agent.run()` is opaque and token data is only fresh inside the step loop. |
| Byte-size trigger | `forceFlushTranscriptBytes` (default 2MB) — forces flush when transcript file exceeds size, independent of token threshold | Not implemented — token-based trigger is sufficient for CUA's single-task sessions |
| CLI/heartbeat guards | `!isHeartbeat && !isCli` — skips flush for heartbeat pings and CLI-mode sessions | Not needed — CUA benchmark always runs as a full agent session |
| Sandbox write check | `memoryFlushWritable` — checks sandbox config allows workspace writes | Not needed — CUA agents always have write access |
| Reactive compaction path | Flush not wired into reactive (exception-based) compaction path | Same — flush only runs on proactive compaction trigger |

### Dropped from OpenClaw

| Feature | Why dropped |
|---------|-------------|
| `forceFlushTranscriptBytes` (2MB byte-size trigger) | CUA sessions are single-task, shorter-lived. Token threshold is sufficient. |
| `resolveMemoryFlushPromptForRun()` date/timezone substitution | CUA agents don't use date-based memory file paths. Memory written via `memory_write` tool. |
| `resolveMemoryFlushSettings()` config system | No equivalent config layer in CUA. Threshold values are function parameters with sensible defaults. |
| `ensureNoReplyHint()` — append silent token hint if missing | Our prompts always include the silent token. |
| `runWithModelFallback()` for flush execution | CUA uses a single model. Fallback adds complexity without benefit. |
| `estimatePromptTokensForMemoryFlush()` | Not needed — we have fresh token estimates from the overflow callback. |
| `readSessionLogSnapshot()` / transcript tail reading for stale token recovery | Not needed — overflow callback always has fresh token estimates. |
| Compaction-during-flush handling | Our flush is a single LLM call, not a full agent run, so compaction can't happen within it. |
| `incrementCompactionCount` after flush-triggered compaction | Same reason — no nested compaction possible. |

## Future Work

### Ratio-based flush threshold for large context windows

Both OpenClaw and our implementation use a hardcoded `reserveTokensFloor` (20K tokens). This works well for typical context windows (~50K–200K), where the flush threshold (`contextWindow - 20K - 4K`) fires comfortably before compaction (80% of context window). However, for large context windows (e.g., 1M tokens), the 20K reserve becomes negligible — flush would trigger at ~976K while compaction fires at 800K, meaning compaction always preempts flush. A future revision could make the reserve ratio-based (e.g., a percentage of context window, or keyed to the compaction threshold) to ensure flush fires before compaction at any scale. Keeping the hardcoded 20K default for now to stay faithful to OpenClaw's design.
