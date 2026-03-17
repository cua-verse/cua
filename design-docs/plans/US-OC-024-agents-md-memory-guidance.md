# US-OC-024: AGENTS.md Memory Guidance Enrichment

**Status**: Draft (pre-planning investigation notes)
**Date**: 2026-03-16

## Problem

The model doesn't know when to write to `task_memory` vs `session` because our AGENTS.md (52 lines) gives minimal guidance. In practice, the model defaults to session writes or skips memory writes entirely.

## OpenClaw Design Rationale

### What OpenClaw Does

OpenClaw's AGENTS.md is a **static, user-authored file** created once from a 220-line template (`openclaw/docs/reference/templates/AGENTS.md`) during `openclaw setup`. It's injected into every session's system prompt as a bootstrap context file under `# Project Context > ## AGENTS.md`.

OpenClaw's memory guidance has three layers:

1. **AGENTS.md template** — teaches the model the memory *philosophy*:
   - Distinguishes "daily notes" (`memory/YYYY-MM-DD.md`, raw logs, append-only) from "long-term memory" (`MEMORY.md`, curated wisdom, overwrite)
   - Explicit trigger mapping: "someone says remember → daily file", "learn a lesson → update AGENTS.md/TOOLS.md"
   - Consolidation guidance: "Over time, review daily files and update MEMORY.md with what's worth keeping"
   - Memory maintenance during heartbeats (periodic curation every few days)

2. **System prompt Memory Recall section** (`system-prompt.ts:buildMemorySection`) — teaches *retrieval* behavior:
   - "Before answering anything about prior work... run memory_search then memory_get"
   - Citation guidance

3. **Memory flush prompt** (`memory-flush.ts`) — teaches *pre-compaction persistence*:
   - "Store durable memories now (use memory/YYYY-MM-DD.md)"
   - Points to daily files by default, not long-term memory

Key insight: **OpenClaw doesn't use a `memory_write` tool**. It uses regular file tools (`write_file`) — the model decides which file to write based on AGENTS.md guidance. Our `memory_write` tool abstracts this into a `target` enum, which is simpler but requires explicit guidance about when to use each target.

### What We Keep and Why

**Two-tier memory distinction** — session logs vs task memory maps directly to OpenClaw's daily notes vs MEMORY.md. The concept is sound; our AGENTS.md just doesn't explain it well enough.

**Explicit trigger mapping** — OpenClaw's "when X → write to Y" examples are the most actionable guidance. The model needs concrete decision rules, not abstract descriptions.

**Consolidation guidance** — "Review session logs and update TASK_MEMORY.md with durable insights" teaches the model to curate. Without this, task_memory never gets written because the model doesn't know it should synthesize.

**"Write It Down — No Mental Notes"** — We already have this section. It works but needs more specificity about *where* to write.

### What We Drop and Why

**MEMORY.md security scoping** — OpenClaw loads MEMORY.md only in "main sessions" (direct chats), never in group contexts. CUA has no multi-session/multi-user concept — every run is a single-agent task. Drop the "ONLY load in main session" guidance.

**Heartbeat-driven memory maintenance** — OpenClaw uses heartbeat polls (periodic cron-like checks) to trigger memory curation. CUA has no heartbeat mechanism — the agent runs continuously for N steps then stops. Instead, guide the model to consolidate during memory flush or at natural task boundaries.

**SOUL.md / USER.md / IDENTITY.md references** — OpenClaw's session startup reads 4+ workspace files. Our agent only has AGENTS.md + TASK_MEMORY.md. Keep startup simple.

**File tool memory writes** — OpenClaw uses `write_file` to memory paths. We use `memory_write` with `target` enum. Our approach is better for CUA because it prevents path confusion and enforces the two-tier structure.

### Key Differences from OpenClaw

| Aspect | OpenClaw | Our CUA adaptation |
|--------|----------|--------------------|
| Write mechanism | `write_file` to any path | `memory_write` tool with `target` enum |
| Memory layout | `memory/YYYY-MM-DD.md` + `MEMORY.md` | `memory/session-NNN.md` + `TASK_MEMORY.md` |
| Session naming | Date-based daily files | Sequential session numbers |
| Curation trigger | Heartbeat polls (periodic cron) | Memory flush (pre-compaction) + agent judgment |
| Multi-session scope | Main session only for MEMORY.md | N/A — single agent per task |
| Template size | 220 lines (covers groups, heartbeats, reactions) | ~80 lines target (task-focused) |

## Proposed Changes

### 1. AGENTS.md Memory Section Rewrite

Current (12 lines):
```markdown
## Memory
You wake up fresh each session. Memory files are your continuity:
- **Task memory** (`TASK_MEMORY.md`) — curated knowledge about this task
- **Session logs** (`memory/session-NNN.md`) — append-only logs
### Write It Down — No "Mental Notes"!
```

Proposed structure (~40 lines):
```markdown
## Memory

You wake up fresh each session. Memory files are your continuity.

### Two Memory Layers

- **Session logs** (`memory/session-NNN.md`) — raw logs of what happened this session
  - Append-only. Write observations, actions taken, errors encountered.
  - Think of these as your scratchpad — capture everything, filter nothing.
  - Use `memory_write` with `target='session'`

- **Task memory** (`TASK_MEMORY.md`) — curated knowledge about this task
  - Your distilled wisdom. Strategies that work, patterns discovered, dead ends to avoid.
  - Overwrites the whole file — always include everything worth keeping.
  - Use `memory_write` with `target='task_memory'`

### When to Write What

| What happened | Write to | Example |
|---|---|---|
| Observed something new | session log | "Floor 3 has a locked door requiring blue key" |
| Tried an action, saw result | session log | "Clicked shop button — opens item purchase menu" |
| Discovered a working strategy | task_memory | "Always buy the iron shield before floor 4 boss" |
| Made a mistake worth avoiding | session log + task_memory | Log the error, update strategy |
| Reached a milestone | session log | "Cleared floor 5, HP=200, have 3 keys" |
| Synthesized lessons from multiple sessions | task_memory | Consolidate patterns into durable guidance |

### Write It Down — No "Mental Notes"!

- "Mental notes" don't survive session restarts. Memory files do.
- When you discover a working strategy → write it to task_memory
- When you observe game state → write it to session log
- When you make a mistake → document it so future-you doesn't repeat it

### Memory Consolidation

Before ending a session or when the context is getting long:
- Review what you've learned this session
- Update TASK_MEMORY.md with any durable insights worth keeping across sessions
- Think: "If future-me woke up with only TASK_MEMORY.md, would they have what they need?"
```

### 2. Memory Flush Prompt Update (optional, minor)

Current flush prompt points only to `target='session'`. Consider updating to:
```
"Store durable memories now. Use memory_write with target='session' for session notes,
or target='task_memory' for strategies and insights worth keeping across sessions."
```

This is a minor change in `session.py` — could be done in this story or deferred.

### 3. No Code Changes Required

The memory tools, memory store, and agent wiring are all correct. This is purely a prompt engineering task — improving the guidance the model receives about *when* and *how* to use the tools it already has.

## Open Questions

1. **Should the memory flush prompt mention task_memory?** OpenClaw's flush prompt only points to daily files. But our model rarely writes to task_memory, so explicitly mentioning it during flush might help.

2. **Should AGENTS.md include game-specific examples?** The trigger mapping table above uses Magic Tower examples. This makes guidance concrete but couples AGENTS.md to one task. Could use generic examples instead and let task-specific guidance come from the task description.

3. **How long should AGENTS.md be?** OpenClaw's template is 220 lines but covers groups, heartbeats, reactions, cron — all irrelevant to CUA. Target ~80 lines (current 52 + ~30 for memory enrichment) to keep bootstrap injection lean.
