# US-OC-002: B3a — Memory Store

## Context

The agent needs persistent memory across sessions. US-OC-001 (System Prompt Builder) is done. This story builds the storage backend that US-OC-003 (Memory Tools) will wrap as CUA BaseTool subclasses.

**Key insight**: OpenClaw uses a **workspace per agent** (`~/.openclaw/workspace/`) containing bootstrap files (`AGENTS.md`, `MEMORY.md`, etc.) loaded into the system prompt every session. We mirror this with a **workspace per task** — each benchmark task gets its own directory with task memory and session logs.

**Bootstrap injection**: `TASK_MEMORY.md` is injected into the system prompt as a ContextFile (like OpenClaw's `MEMORY.md` via `loadWorkspaceBootstrapFiles()` → `buildBootstrapContextFiles()`). This gives the agent accumulated knowledge at session start without a tool call.

## OpenClaw Design Rationale

### What OpenClaw Does
OpenClaw workspace (`~/.openclaw/workspace/`):
- `AGENTS.md` — operating instructions (loaded every session)
- `MEMORY.md` — curated long-term memory (loaded every session as bootstrap context)
- `memory/YYYY-MM-DD.md` — daily logs (today + yesterday read at session start)
- Memory search via SQLite + FTS5 + vector embeddings (hybrid BM25 + cosine)

Reference: `openclaw/src/memory/manager.ts`, `openclaw/src/agents/workspace.ts`, `openclaw/docs/concepts/memory.md`, `openclaw/docs/concepts/agent-workspace.md`

### What We Keep and Why
1. **Workspace-per-entity pattern** — OpenClaw: workspace per agent. CUA: workspace per task. Each benchmark task is independent; task isolation is natural.
2. **Bootstrap injection of curated memory** — `TASK_MEMORY.md` loaded into system prompt like OpenClaw's `MEMORY.md`. Agent starts each session with accumulated knowledge.
3. **Session logs** — `memory/session-NNN.md` (≈ OpenClaw's `memory/YYYY-MM-DD.md`). Auto-incrementing, append-only.
4. **Keyword search** — case-insensitive substring matching across task files. Sufficient for agent-generated queries in short benchmark runs.
5. **Graceful degradation** — missing files return empty strings (matches OpenClaw's `memory_get` behavior).

### What We Drop and Why
1. **Global memory across tasks** — CUA tasks are isolated benchmarks. No root-level `MEMORY.md`.
2. **Daily log pattern** — benchmarks run in sessions, not days. `session-NNN` replaces `YYYY-MM-DD`.
3. **SQLite/vector/hybrid search** — files are small (50-100 step runs), keyword search is sufficient. May revisit in a future PRD if memory corpus grows.
4. **File watching, chunking, temporal decay, MMR** — overkill for small task-scoped files. May revisit in a future PRD for longer-running tasks.
5. **Session transcript indexing** — CUA's TrajectorySaverCallback handles trajectory persistence separately. May revisit in a future PRD for cross-session search.

### Key Differences from OpenClaw
- **Task-scoped workspaces** instead of agent-scoped workspaces
- **No database** — pure filesystem
- **Keyword-only search** — no embeddings
- **Session numbering** instead of date-based daily logs

## Task Workspace Layout

```
<base_dir>/tasks/<task_id>/
├── TASK_MEMORY.md              # Curated task knowledge (≈ MEMORY.md)
└── memory/
    ├── session-001.md          # Session log (append-only)
    ├── session-002.md
    └── ...
```

## Implementation Plan

### 1. NEW: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/memory.py`

**SearchResult** dataclass + **MemoryStore** class. Reference: `memory/store.py` (API shape), `openclaw/src/memory/` (design patterns).

API:
- `MemoryStore(base_dir, task_id)` — task_id required
- `task_dir` → `base_dir / tasks / task_id`
- `memory_dir` → `task_dir / memory`
- `init_session()` → creates `memory/session-NNN.md`, returns relative path
- `append_to_session_log(content)` → appends timestamped entry
- `write_task_memory(content)` → overwrites `TASK_MEMORY.md`
- `read_task_memory()` → reads (empty string if missing)
- `read_file(relative_path, start_line, end_line)` → line-range read
- `search(keywords, max_results)` → keyword search across TASK_MEMORY.md + memory/session-*.md
- `list_session_files()` → sorted relative paths
- `get_bootstrap_context()` → TASK_MEMORY.md content for ContextFile injection

### 2. MODIFY: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/__init__.py`

Add exports: `MemoryStore`, `SearchResult`.

### 3. NEW: `tests/test_openclaw_memory_store.py`

Port from `tests/test_memory_store.py` with updated imports and layout (`memory/` subdirectory for sessions).

### 4. MODIFY: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/AGENTS.md`

Revise to reflect task workspace layout:
- Session Startup: read `TASK_MEMORY.md` (already injected in system prompt) + latest session log
- Memory section: `TASK_MEMORY.md` for curated knowledge, `memory/session-NNN.md` for session logs
- Remove global MEMORY.md/daily log references
- Note write mechanism depends on US-OC-003

### 5. MODIFY: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/prompt.py`

Update Memory Recall section to reference `TASK_MEMORY.md` + `memory/session-*.md` instead of `MEMORY.md + memory/*.md`.

### Design Decisions (added during implementation)

**Current Date & Time in system prompt**: The PromptBuilder now includes a "Current Date & Time" section (UTC) injected at build time, mirroring OpenClaw's `system-prompt.ts` which embeds the current timestamp so the agent knows the date/time without a tool call. Controlled by `PromptConfig.time` (enabled by default). Reference: OpenClaw SYSTEM_PROMPT.md "Current Date & Time" section.

**Two most recent session logs at startup**: AGENTS.md instructs the agent to read the two most recent session logs (not just one) at session start, mirroring OpenClaw's pattern of reading "today + yesterday" daily logs for broader recent context.

## Verification

1. **Lint**: `uv run ruff check submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/memory.py tests/test_openclaw_memory_store.py`
2. **Tests**: `uv run pytest tests/test_openclaw_memory_store.py -v`
3. **Import**: `python -c "from cua_bench.agents.openclaw import MemoryStore"`
4. Check acceptance criteria against PRD US-OC-002
