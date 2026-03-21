# US-OC-024: AGENTS.md Memory Guidance Enrichment

**Status**: Implemented
**Date**: 2026-03-20

## Problem

The model defaults to session writes and rarely writes to `task_memory` because AGENTS.md (52 lines) gives minimal guidance on the two-tier distinction. OpenClaw teaches memory behavior through three coordinated layers — our gap analysis identified where we fall short.

## OpenClaw Design Rationale

### What OpenClaw Does

OpenClaw's memory guidance has three layers:

1. **AGENTS.md template** (220 lines) — teaches memory *philosophy*:
   - Distinguishes "daily notes" (`memory/YYYY-MM-DD.md`, raw logs) from "long-term memory" (`MEMORY.md`, curated wisdom)
   - Explicit trigger mapping: "someone says remember → daily file", "learn a lesson → update relevant file"
   - Consolidation guidance: "Review daily files and update MEMORY.md with what's worth keeping"
   - Memory maintenance during heartbeats (periodic curation)

2. **System prompt Memory Recall section** (`buildMemorySection()`) — teaches *retrieval*:
   - "Before answering anything about prior work... run memory_search then memory_get"
   - Citation guidance

3. **Memory flush prompt** (`memory-flush.ts`) — teaches *pre-compaction persistence*:
   - Points to daily files by default

### Gap Analysis

| Layer | OpenClaw | Our Implementation | Gap |
|-------|----------|-------------------|-----|
| AGENTS.md two-tier distinction | Rich behavioral descriptions | 2 lines each, no guidance | **Major** |
| AGENTS.md trigger mapping | Concrete "when X → write Y" rules | None | **Major** |
| AGENTS.md consolidation | "Review and update MEMORY.md" | None | **Major** |
| AGENTS.md heartbeats | Periodic curation cron | N/A — CUA has no heartbeats | Drop |
| Memory Recall (prompt.py) | Search-first + citations | Same pattern | None |
| Flush prompt (session.py) | Points to daily files | Points only to `target='session'` | **Minor** |

### What We Keep and Why

- **Two-tier memory distinction** — session logs vs task memory maps directly to OpenClaw's daily notes vs MEMORY.md
- **Explicit trigger mapping** — the most actionable guidance; model needs decision rules, not abstractions
- **Consolidation guidance** — without it, task_memory never gets written
- **"Write It Down — No Mental Notes"** — already exists, updated with target-specific guidance

### What We Drop and Why

- **MEMORY.md security scoping** — CUA has no multi-session/multi-user concept
- **Heartbeat-driven curation** — CUA runs continuously then stops; consolidate during flush or at task boundaries
- **SOUL.md / USER.md / IDENTITY.md** — our agent only has AGENTS.md + TASK_MEMORY.md
- **File tool writes** — we use `memory_write` with `target` enum (better for CUA: prevents path confusion)

### Key Differences from OpenClaw

| Aspect | OpenClaw | Our CUA adaptation |
|--------|----------|--------------------|
| Write mechanism | `write_file` to any path | `memory_write` with `target` enum |
| Memory layout | `memory/YYYY-MM-DD.md` + `MEMORY.md` | `memory/session-NNN.md` + `TASK_MEMORY.md` |
| Session naming | Date-based daily files | Sequential session numbers |
| Curation trigger | Heartbeat polls (periodic cron) | Memory flush + agent judgment |
| Template size | 220 lines (groups, heartbeats, reactions) | 78 lines (task-focused) |

## Changes Made

### 1. AGENTS.md Memory Section Rewrite (52 → 78 lines)

**File**: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/AGENTS.md`

Added four subsections to the Memory section:
- **Two Memory Layers** — explicit descriptions of session logs (scratchpad, append-only) vs task memory (curated wisdom, overwrite), with `target=` usage
- **When to Write What** — 6-row trigger mapping table with generic examples (not game-specific, so AGENTS.md works across all tasks)
- **Write It Down** — updated to reference specific targets (`task_memory` for strategies, session log for observations)
- **Memory Consolidation** — guidance to synthesize durable insights before session end

### 2. Memory Flush Prompt Update

**File**: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/session.py` (line 496)

Updated `MEMORY_FLUSH_PROMPT` to mention both targets:
```python
"Store durable memories now (use memory_write with target='session' for observations, "
"or target='task_memory' for strategies and insights worth keeping across sessions). "
```

### 3. No other changes needed

- `prompt.py` Memory Recall section already matches OpenClaw's `buildMemorySection()`
- Memory tools, memory store, agent wiring all correct

## Resolved Open Questions

1. **Flush prompt mentions task_memory** — yes, added. OpenClaw's flush only points to daily files, but our model rarely writes to task_memory, so explicit mention helps.
2. **Generic examples in AGENTS.md** — yes, used generic UI examples instead of game-specific ones. Task descriptions provide domain context.
3. **Target length ~78 lines** — well under OpenClaw's 220 (which covers irrelevant features like groups, heartbeats, reactions).

## Verification

- Level 1: `uv run ruff check submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/session.py` — passes
- Level 1: AGENTS.md has two-tier distinction, trigger mapping table (6 rows), consolidation guidance
- Level 1: Flush prompt mentions both `target='session'` and `target='task_memory'`
- Level 2: `run_magic_tower.sh 50` — agent writes to both session log AND task_memory at least once
