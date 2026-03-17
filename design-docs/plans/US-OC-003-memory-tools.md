# US-OC-003: Memory Tools — Implementation Plan

## Context

US-OC-002 delivered `MemoryStore` in `agents/openclaw/memory.py` with task-scoped storage (TASK_MEMORY.md + session logs). CUA agents lack direct file I/O, so they need dedicated BaseTool subclasses to search, read, and write memory files during task execution. This story provides those tools.

## OpenClaw Design Rationale

### What OpenClaw Does
- `memory_search`: semantic search via embeddings (SQLite + vector), returns scored snippets with citations
- `memory_get`: safe file read with `path`/`from`/`lines` params, .md-only
- No dedicated write tool — agents write via generic filesystem tools (write_to_file, edit_file)

### What We Keep and Why
- **Two read tools** (search + get): Same search-then-read flow. Agent searches first, then reads specific lines. Keeps context small.
- **Tool schemas**: Same parameter names (`query`, `path`, `from`, `lines`, `maxResults`) for API familiarity
- **Security guards**: Path traversal rejection, .md-only restriction on get
- **Graceful error handling**: Missing files return empty string, not exceptions

### What We Drop and Why
- **Embedding/vector search**: Our MemoryStore uses keyword search (sufficient for task-scoped files). No SQLite, no chunking, no temporal decay.
- **Citations system**: CUA agents consume plain text, not structured citations
- **Provider abstraction**: Single backend (filesystem), no fallback/probe logic
- **minScore parameter**: Keyword scores are integer counts, not continuous. May revisit with BM25.

### What We Add and Why
- **memory_write tool**: CUA agents have no generic file I/O. Dedicated tool with two targets: `session` (append to session log) and `task_memory` (overwrite TASK_MEMORY.md). This replaces OpenClaw's filesystem access pattern.
- **keywords parameter on search**: Convenience alternative to `query` string — avoids whitespace splitting ambiguity

### Key Differences from OpenClaw
- Search is keyword-based, not semantic — results sorted by match count
- Write is explicit tool, not filesystem access
- No global MEMORY.md — everything is task-scoped
- Two write targets instead of three (no global "memory" target)

## Implementation

### File: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/memory.py`

Append three BaseTool subclasses below the existing `MemoryStore` class:

1. **MemorySearchTool** (`@register_tool("memory_search")`)
   - Constructor: `(store: MemoryStore, cfg=None)`
   - Params: `query` (string), `keywords` (list[str]), `max_results` (int, default 10)
   - Behavior: resolve keywords from `query` or `keywords`, call `store.search()`, format as `[path:line] (score N) content`
   - Errors: missing keywords → ValueError; store errors → friendly message

2. **MemoryGetTool** (`@register_tool("memory_get")`)
   - Constructor: `(store: MemoryStore, cfg=None)`
   - Params: `path` (string, required), `from` (int), `lines` (int)
   - Security: reject `..` and absolute paths, reject non-`.md` files
   - Behavior: call `store.read_file(path, start_line, end_line)`, return content or "not found"

3. **MemoryWriteTool** (`@register_tool("memory_write")`)
   - Constructor: `(store: MemoryStore, cfg=None)`
   - Params: `content` (string, required), `target` (enum: "session", "task_memory")
   - Behavior: dispatch to `store.append_to_session_log()` or `store.write_task_memory()`
   - Errors: empty content → error; no session init → error; invalid target → error

### File: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/__init__.py`

Add exports: `MemorySearchTool`, `MemoryGetTool`, `MemoryWriteTool`

### File: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/AGENTS.md`

Update the Memory section note to remove the "requires US-OC-003" caveat — tools are now available. Reference the three tool names: `memory_search`, `memory_get`, `memory_write`.

### File: `tests/test_openclaw_memory_tools.py` (new)

Test structure mirroring `tests/test_memory_tools.py` but adapted for the new MemoryStore API:

- **TestMemorySearchTool**: name, schema, basic search, no results, multiple keywords, max_results, JSON string params, missing keywords error, empty keywords error, query string splitting, keywords precedence, store error handling
- **TestMemoryGetTool**: name, schema, full file read, line range, path traversal rejection, absolute path rejection, non-md rejection, missing file, JSON string params
- **TestMemoryWriteTool**: name, schema, write session, write task_memory, empty content rejected, whitespace rejected, default target is session, no session init error, invalid target rejected, JSON string params

## Verification

1. `uv run ruff check submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/memory.py tests/test_openclaw_memory_tools.py`
2. `uv run pytest tests/test_openclaw_memory_tools.py -v`
3. All tests pass, lint clean
