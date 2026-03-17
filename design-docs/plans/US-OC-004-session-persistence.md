# US-OC-004: Session Persistence — Implementation Plan

## Context

The OpenClaw agent harness currently has no persistence — each `perform_task()` call starts fresh. US-OC-004 adds a `SessionManager` that persists cross-run state and JSONL transcripts, reproducing OpenClaw's session persistence adapted for CUA.

---

## OpenClaw Design Rationale

### What OpenClaw Does
OpenClaw persists sessions via two layers:
- **sessions.json** — flat map of session keys → entries (60+ fields: routing, tokens, compaction count, model overrides, delivery metadata)
- **JSONL transcripts** — one entry per line, typed events forming a parentId-linked chain

**Complete OpenClaw JSONL entry types** (10 types, from source at `openclaw/src/config/sessions/transcript.ts` and `pi-embedded-runner/tool-result-truncation.ts`):

All entries share: `type`, `id`, `parentId`, `timestamp` — forming a linked chain.

| # | Entry Type | Key Fields | Purpose | CUA |
|---|-----------|-----------|---------|-----|
| 1 | `session` | version, id, timestamp, cwd | File header (1 per file) | **KEEP** |
| 2 | `message` | message.{role, content[], usage, api, provider, model, stopReason} | User/assistant/tool messages | **KEEP** |
| 3 | `compaction` | summary, firstKeptEntryId, tokensBefore, details?, fromHook? | Context compaction summary | **KEEP** |
| 4 | `model_change` | provider, modelId | Model/provider switch mid-session | DROP |
| 5 | `thinking_level_change` | thinkingLevel | Extended thinking toggle | DROP |
| 6 | `custom` | customType, data | Extensible events (cache-ttl, model-snapshot, prompt-error, google-turn-ordering) | DROP |
| 7 | `custom_message` | customType, content[], display?, details? | Custom displayable messages (notifications, memory updates) | DROP |
| 8 | `branch_summary` | (entry ID references) | Conversation branching marker | DROP |
| 9 | `label` | (entry ID references) | Entry annotation/tagging | DROP |
| 10 | `session_info` | name? | Session metadata updates | DROP |

**We keep 3 types**: `session` (header), `message` (conversation), `compaction` (context management).
**We drop 7 types**: All are UI/multi-model/provider-specific features irrelevant to CUA's single-model benchmark runs.

### JSONL Transcript vs CUA Trajectory Logs

CUA's TrajectorySaverCallback already saves per-turn API requests/responses/screenshots. Why add JSONL?

| Aspect | CUA Trajectory | OpenClaw-style JSONL |
|--------|---------------|---------------------|
| Format | Directory tree (N×4 files per turn) | Single append-only file |
| Load previous run | Parse N turn dirs, reassemble | Read one file sequentially |
| Content | Full API dumps + screenshots | Conversation messages only (lightweight) |
| Events | Messages only | Messages + model changes + config + custom |
| Branching | Flat sequence | ParentId chain (supports compaction branches) |
| Cost tracking | Token counts only | Per-message dollar costs |
| Size | Large (includes base64 screenshots) | Small (text only) |

**Bottom line**: Trajectories are for debugging/evaluation. JSONL is the conversation-oriented persistence layer optimized for loading into the next run's context and tracking compaction history.

### What We Keep and Why

| Component | Why |
|-----------|-----|
| State JSON (cross-run metadata) | Track run_number, cumulative tokens, compaction summaries — no CUA equivalent |
| JSONL `session` entry | Mark file version and run context (header) |
| JSONL `message` entry with parentId chain | Single-file conversation stream with per-message usage/cost tracking |
| JSONL `compaction` entry | Persist compaction summaries inline with firstKeptEntryId for history reconstruction |
| ParentId chain across all entries | Track conversation flow and compaction branch points |

### What We Drop and Why

| Component | Rationale |
|-----------|-----------|
| Session keys/routing (60+ fields) | Single-task, single-agent — no multi-user routing |
| 7 JSONL entry types (model_change, thinking_level_change, custom, custom_message, branch_summary, label, session_info) | Single model, no UI, no branching — see table above |
| Maintenance (pruning, archival, disk budgets) | Bounded by benchmark runs |
| Atomic writes / cache TTL | Single writer, no concurrency |
| Send policy, queue mode, heartbeats | UI/delivery features |

### Key Differences from OpenClaw

1. **3 of 10 entry types**: Keep `session`, `message`, `compaction` — drop 7 UI/multi-model types
2. **Single file per task**: Like OpenClaw's single JSONL per session — `session` header entries mark run boundaries
3. **Explicit run numbers**: Auto-incrementing in state.json vs OpenClaw's timestamp-based tracking
4. **Task-scoped**: `sessions_dir/<task_id>/` vs OpenClaw's agent-scoped routing keys
5. **Compaction summaries in state.json**: Also available as list for easy injection (in addition to being inline in JSONL)

---

## Storage Layout

```
openclaw_sessions/                          # DEFAULT_BASE_DIR
└── <task_id>/
    ├── state.json                          # Cross-run metadata
    └── transcript.jsonl                    # ALL runs, append-only (session headers mark run boundaries)

# CUA trajectories remain at their existing location (unchanged):
logging_dir/trajectories/<trajectory_id>/   # Debugging/evaluation
```

A `session` header entry marks the start of each run. This matches OpenClaw's single-file-per-session model and keeps the full task history in one place.

### state.json Schema

```json
{
  "task_id": "mota_24_easy",
  "run_number": 2,
  "step_count": 47,
  "total_tokens": {"prompt_tokens": 125000, "completion_tokens": 8500},
  "compaction_count": 1,
  "compaction_summaries": ["Summary of context before compaction..."],
  "created_at": "2026-03-11T10:00:00Z",
  "updated_at": "2026-03-11T10:15:00Z"
}
```

### JSONL Entry Types

**1. Session header** (marks start of each run within the single transcript file):
```json
{"type": "session", "id": "sess-uuid", "parentId": null, "timestamp": "2026-03-11T10:00:00Z", "version": 1, "task_id": "mota_24_easy", "run_number": 1, "model": "anthropic/claude-sonnet-4-20250514"}
```

**2. Message** (user input, assistant response, tool results):
```json
{"type": "message", "id": "msg-uuid", "parentId": "prev-id", "timestamp": "2026-03-11T10:01:23Z", "message": {"role": "assistant", "content": "I'll click on the door.", "usage": {"input": 2100, "output": 45, "total": 2145, "cost": 0.0034}, "stopReason": "tool_use"}}
```

```json
{"type": "message", "id": "msg-uuid2", "parentId": "msg-uuid", "timestamp": "2026-03-11T10:01:24Z", "message": {"role": "user", "content": "[tool_result] screenshot captured"}}
```

**3. Compaction** (context was summarized — references OpenClaw's `compaction.ts`):
```json
{"type": "compaction", "id": "cmp-uuid", "parentId": "msg-uuid2", "timestamp": "2026-03-11T10:05:00Z", "summary": "Agent navigated to floor 2, defeated slime enemies...", "firstKeptEntryId": "msg-uuid-N", "tokensBefore": 95000}
```

---

## Implementation

### File 1 (NEW): `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/session.py`

**Dataclasses:**
- `TokenUsage(prompt_tokens=0, completion_tokens=0)`
- `SessionState(task_id, run_number, step_count, total_tokens, compaction_count, compaction_summaries, created_at, updated_at)`
- `TranscriptEntry(type, id, parent_id, timestamp, ...)` — mirrors OpenClaw's typed entries; discriminated by `type`:
  - `session`: + version, task_id, model
  - `message`: + message.{role, content, usage?, stopReason?}
  - `compaction`: + summary, firstKeptEntryId, tokensBefore

**SessionManager class:**
- `DEFAULT_BASE_DIR = "openclaw_sessions"` (None sentinel pattern from MemoryStore)
- Path properties: `task_dir`, `state_path`, `transcript_path`
- `init_session()` → load existing state, increment run_number, reset step_count, preserve cumulative tokens + compaction summaries, append `session` header to transcript.jsonl
- `save_state()` / `load_state()` → JSON round-trip
- `append_message(role, content, usage?, stop_reason?)` → append `message` entry with auto-generated id + parentId chain
- `append_compaction(summary, first_kept_entry_id, tokens_before)` → append `compaction` entry inline
- `load_history(run_number?)` → load entries from transcript.jsonl, optionally filtered to a specific run (default: all)
- `update_tokens(prompt, completion)` / `update_step_count(step)` → accumulate and persist
- `add_compaction_summary(summary)` → also store in state.json list for easy cross-run injection
- `get_compaction_summaries()` → return summaries from state.json

### File 2 (MODIFY): `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/__init__.py`

Add exports: `SessionManager`, `SessionState`, `TokenUsage`, `TranscriptEntry`

### File 3 (NEW): `tests/test_openclaw_session.py`

~28 tests, `tmp_path` fixture, class-per-feature:
- **TestTokenUsage** — defaults, accumulation
- **TestSessionState** — serialization roundtrip
- **TestTranscriptEntry** — header entry, message entry, JSON serialization
- **TestSessionManagerInit** — paths, DEFAULT_BASE_DIR patching
- **TestInitSession** — create (run=1), increment, preserve tokens/compaction, reset step_count, appends session header to transcript
- **TestSaveLoadState** — roundtrip, missing → None, corrupt → None, updated_at
- **TestAppendMessage** — single, multiple, parentId chain, usage tracking
- **TestLoadHistory** — read all entries, filter by run number, empty transcript → []
- **TestUpdateTokens** — accumulation, persistence
- **TestCompaction** — add summary, increment count, get summaries

### NOT modified: `openclaw_agent.py`

Agent wiring is US-OC-008.

---

## Verification

1. **Lint**: `uv run ruff check submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/session.py tests/test_openclaw_session.py`
2. **Tests**: `uv run pytest tests/test_openclaw_session.py -v`
3. **Existing tests**: `uv run pytest tests/ -v`
