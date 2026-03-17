# OpenClaw Source Code Analysis

<!-- Last updated: 2026-03-10 -->

Reference analysis of OpenClaw's actual TypeScript implementation at `openclaw/src/`. Used as the primary source-of-truth for openclaw agent reproduction (more authoritative than concept docs, which simplify).

## Directory Structure

```
openclaw/src/
├── agents/                    # Agent runtime, tools, system prompt, compaction
│   ├── compaction.ts          # Compaction pipeline (summarization, chunking, retry)
│   ├── system-prompt.ts       # System prompt construction (12+ sections)
│   ├── context-window-guard.ts # Context window monitoring
│   ├── memory-search.ts       # Memory search tool config + resolution
│   ├── lanes.ts               # Per-session execution queues
│   ├── pi-embedded-runner.ts  # Main agent runner (4-phase orchestration)
│   ├── pi-embedded-runner/
│   │   ├── run.ts             # runEmbeddedPiAgent() entry point (1396 lines)
│   │   └── compact.ts         # Session compaction orchestration
│   ├── bootstrap-*.ts         # Bootstrap file loading, budget, caching, hooks
│   ├── pi-tools.*.ts          # Tool definitions (read, write, edit, exec, etc.)
│   └── openclaw-tools.ts      # OpenClaw-specific tools (sessions, camera, etc.)
│
├── memory/                    # Memory system (SQLite + embeddings)
│   ├── manager.ts             # MemoryIndexManager singleton (786 lines)
│   ├── manager-search.ts      # Vector + keyword search implementation
│   ├── manager-embedding-ops.ts # Embedding pipeline
│   ├── manager-sync-ops.ts    # File sync + hash-based diffing
│   ├── hybrid.ts              # BM25 + vector merge (150 lines)
│   ├── mmr.ts                 # Maximal Marginal Relevance diversity reranking
│   ├── temporal-decay.ts      # Exponential recency decay
│   ├── embeddings.ts          # Embedding provider abstraction
│   ├── embeddings-openai.ts   # OpenAI embedding client
│   ├── sqlite.ts              # SQLite schema + operations
│   ├── index.ts               # Chunking + indexing pipeline
│   └── qmd-manager.ts         # QMD sidecar (experimental)
│
├── sessions/                  # Session persistence
├── gateway/                   # WebSocket gateway (clients, protocol)
├── providers/                 # LLM providers (OpenAI, Anthropic, Google, etc.)
├── commands/                  # CLI commands (/model, /compact, etc.)
└── channels/                  # Channel integrations (telegram, discord, slack, etc.)
```

## 1. Compaction Pipeline (`agents/compaction.ts` — 465 lines)

### Key Constants

| Constant | Value | Purpose |
|----------|-------|---------|
| `BASE_CHUNK_RATIO` | 0.4 | Chunk size relative to total context |
| `MIN_CHUNK_RATIO` | 0.15 | Minimum ratio when messages are large |
| `SAFETY_MARGIN` | 1.2 | 20% token estimation buffer |
| `SUMMARIZATION_OVERHEAD_TOKENS` | 4096 | Reserved for summarization prompts + reasoning |

### Core Algorithm

```
1. splitMessagesByTokenShare(messages, parts)
   → Divides messages into N parts by TOKEN WEIGHT (not count)
   → Targets totalTokens/parts per chunk

2. computeAdaptiveChunkRatio(messages, contextWindow)
   → Reduces chunk ratio when avg message > 10% of context
   → Returns max(MIN_CHUNK_RATIO, BASE_CHUNK_RATIO * adjustment)

3. chunkMessagesByMaxTokens(messages, maxTokens)
   → Applies SAFETY_MARGIN to prevent overflow
   → Forces chunk boundary on oversized messages

4. summarizeChunks(chunks, model, options)
   → Iterative summarization with retry (3 attempts, 500-5000ms delays)
   → Each chunk summarized independently, then merged

5. summarizeWithFallback(messages, model, options)
   → Graceful degradation:
     full messages → exclude oversized → size-only report

6. summarizeInStages(messages, model, options)
   → For very long histories: split → summarize parts → merge summaries
```

### Summarization Prompts (Verbatim Patterns)

**Single-chunk prompt**: Asks to summarize conversation preserving: active tasks, decisions, TODOs, open questions, constraints, commitments.

**Merge prompt** (when multiple partial summaries exist): Emphasizes: active tasks, batch progress, last user request, decisions, TODOs, open questions, constraints, commitments.

**Identifier preservation** (security-critical):
> "Preserve all opaque identifiers exactly as written (no shortening or reconstruction), including UUIDs, hashes, IDs, tokens, API keys, hostnames, IPs, ports, URLs, and file names."

Three policies: `"strict"` (enforce), `"custom"` (user override), `"off"` (disabled).

### Repair Logic

`pruneHistoryForContextShare()` — removes oldest messages while repairing `tool_use`/`tool_result` pairing (orphaned tool results are removed).

## 2. System Prompt Construction (`agents/system-prompt.ts` — 725 lines)

### Prompt Modes

| Mode | Sections | Use case |
|------|----------|----------|
| `full` | All 12+ sections | Primary agent |
| `minimal` | Tooling, workspace, runtime only | Sub-agents |
| `none` | Just identity line | Bare minimum |

### Section Order (full mode)

1. **Tooling** — 30+ tools with pre-defined order: read, write, edit, grep, find, ls, exec, process, web_search, web_fetch, browser, canvas, nodes, cron, message, sessions_*, subagents, image, etc.
2. **Safety** — No independent goals, no self-preservation, prioritize safety over completion
3. **Skills** — Available skills (name + description + location)
4. **OpenClaw Self-Update** — How to run `config.apply` and `update.run`
5. **Workspace** — Working directory location
6. **Documentation** — Local OpenClaw docs path
7. **Workspace Files (Bootstrap)** — Injected file contents under "Project Context":
   - `AGENTS.md`, `SOUL.md`, `TOOLS.md`, `IDENTITY.md`, `USER.md`
   - `HEARTBEAT.md`, `BOOT.md`, `BOOTSTRAP.md` (optional)
   - `MEMORY.md` (main session only)
8. **Sandbox** — Container runtime info (when enabled)
9. **Current Date & Time** — User timezone (stable for prompt caching)
10. **Reply Tags** — `[[reply_to_current]]` or `[[reply_to:<id>]]` syntax
11. **Heartbeats** — Heartbeat prompt + ack behavior
12. **Runtime** — `agent={id} | host={host} | repo={root} | os={os} | model={model} | channel={channel} | capabilities={list} | thinking={level}`
13. **Reasoning** — Visibility level + `/reasoning` toggle

### Memory Recall Section (`buildMemorySection()`)

Injected only when memory tools are available:
- Categorical rule: "Before answering anything about prior work, decisions, dates, people, preferences, or todos: run memory_search"
- Two-step workflow: search first, then targeted read via memory_get
- Failure guidance: "If low confidence after search, say you checked"
- Citations mode support

### Bootstrap Trimming

- Per-file limit: `bootstrapMaxChars` (default 20,000)
- Total limit: `bootstrapTotalMaxChars` (default 150,000)
- Truncation warning: `bootstrapPromptTruncationWarning` (`off|once|always`)

## 3. Context Window Guard (`agents/context-window-guard.ts` — 75 lines)

### Constants

| Constant | Value | Purpose |
|----------|-------|---------|
| `CONTEXT_WINDOW_HARD_MIN_TOKENS` | 16,000 | Blocks execution below this |
| `CONTEXT_WINDOW_WARN_BELOW_TOKENS` | 32,000 | Warning threshold |

### Resolution Precedence

1. `config.models.providers[provider].models[modelId].contextWindow`
2. `model.contextWindow` (from provider catalog)
3. `defaultTokens` parameter
4. Cap by `config.agents.defaults.contextTokens` if present

## 4. Main Agent Runner (`pi-embedded-runner/run.ts` — 1396 lines)

### Entry Point

```typescript
runEmbeddedPiAgent(params: RunEmbeddedPiAgentParams) → Promise<EmbeddedPiRunResult>
```

### 4-Phase Execution

**Phase 1: Setup**
- Resolve workspace, provider, model (with `before_model_resolve` hook)
- Validate context window (warn < 32K, block < 16K)
- Load auth profiles

**Phase 2: Auth Profile Retry Loop (Outer)**
- Iterate through profile candidates
- Max retries: `min(160, max(32, 24 + 8 * profileCount))`
- Per-profile cooldown tracking

**Phase 3: Thinking Level + Failover Retry Loop (Inner)**
- Attempt with thinking level (high → medium → low → off on failure)
- Error classification: auth, billing, rate_limit, context_overflow, timeout
- Fallback to next model on `FailoverError`

**Phase 4: Per-Attempt Execution**
- `runEmbeddedAttempt()` with prepared payloads
- Accumulate usage (input, output, cacheRead, cacheWrite)
- Handle oversized tool results (truncation)
- Apply compaction on context overflow + retry

### Usage Correction (Issue #13698)

Track LAST API call's cache fields (not accumulated totals) to prevent N × context_size inflation:
```
lastPromptTokens = lastInput + lastCacheRead + lastCacheWrite
```

### Session Compaction Orchestration (`compact.ts`)

```
1. Load session via SessionManager
2. Build system prompt (bootstrap files + config)
3. Load/validate message history
4. Split tools (tooling vs action)
5. Execute compaction with safety timeout
6. Replace old messages with single summary
7. Save back to sessionFile
```

Safety: dual-lock (session write lock + file mutex), `before_compact`/`after_compact` hooks, readonly DB recovery.

## 5. Memory System (`memory/`)

### Storage

- SQLite database at `~/.openclaw/memory/<agentId>.sqlite`
- Tables: `files`, `chunks`, `chunks_fts` (FTS5), `chunks_vec` (vector), `embedding_cache`
- Files tracked: `MEMORY.md`, `memory/*.md` (daily logs), session transcripts

### Chunking

```
Document (1200 tokens)
  ├─ Chunk 1: tokens   1-400  (lines 1-10)
  ├─ Chunk 2: tokens 320-720  (lines 8-18)   ← 80-token overlap
  └─ Chunk 3: tokens 640-1040 (lines 16-26)  ← 80-token overlap
```

Default: 400 tokens/chunk, 80 tokens overlap. Token estimation: 4 chars ≈ 1 token.

### Search (`manager-search.ts` — 192 lines)

**Vector search** (SQL):
```sql
SELECT c.id, c.path, c.start_line, c.end_line, c.text, c.source,
       vec_distance_cosine(v.embedding, ?) AS dist
  FROM chunks_vec v
  JOIN chunks c ON c.id = v.id
 WHERE c.model = ? AND {sourceFilter}
 ORDER BY dist ASC LIMIT ?
```
Score = 1 - distance (cosine similarity).

**Keyword search** (FTS5 + BM25):
```sql
SELECT id, path, source, start_line, end_line, text,
       bm25({table}) AS rank
  FROM {ftsTable}
 WHERE {ftsTable} MATCH ?
 ORDER BY rank ASC LIMIT ?
```
Query parsing: `"fix the API"` → `"fix" AND "the" AND "API"` (quoted AND-joined). BM25 score: `1 / (1 + rank)`.

**Hybrid merge** (`hybrid.ts`):
1. Merge by chunk ID (combine vector + keyword scores)
2. `hybridScore = vectorWeight * vectorScore + textWeight * textScore`
3. Apply temporal decay (optional): `score × e^(-λ × ageInDays)`, half-life 30 days
4. Apply MMR (optional): `λ × relevance − (1−λ) × max_similarity_to_selected`, λ=0.7
5. Sort descending, return top-N

### Memory Search Config (`agents/memory-search.ts` — 366 lines)

```typescript
ResolvedMemorySearchConfig = {
  enabled: boolean
  sources: ("memory" | "sessions")[]
  provider: "openai" | "local" | "gemini" | "voyage" | "mistral" | "ollama" | "auto"
  fallback: provider | "none"
  model: string  // e.g. "text-embedding-3-small"
  store: { driver: "sqlite", path: string, vector: { enabled, extensionPath? } }
  chunking: { tokens: 400, overlap: 80 }
  sync: { onSessionStart, onSearch, watch, watchDebounceMs: 1500, intervalMinutes }
  query: {
    maxResults: 6
    minScore: 0.35
    hybrid: {
      enabled: true
      vectorWeight: 0.7
      textWeight: 0.3        // normalized to sum=1
      candidateMultiplier: 4  // fetch 4×maxResults, rerank
      mmr: { enabled: false, lambda: 0.7 }
      temporalDecay: { enabled: false, halfLifeDays: 30 }
    }
  }
  cache: { enabled: true, maxEntries?: number }
}
```

### Memory Manager (`manager.ts` — 786 lines)

Singleton factory: `MemoryIndexManager.get({ cfg, agentId })`. Cache key: `${agentId}:${workspaceDir}:${settingsHash}`.

Key methods:
- **warmSession()** — sync if `onSessionStart=true`; tracks warmed sessions
- **search()** — FTS-only (no provider) or hybrid mode
- **sync()** — deduplicates concurrent syncs; readonly DB recovery
- **readFile()** — .md files with line range; validates path inside workspace
- **close()** — cleanup timers, watcher, DB, remove from cache

### Embedding Providers

| Provider | Default Model | Notes |
|----------|--------------|-------|
| OpenAI | text-embedding-3-small | Batch API support |
| Gemini | gemini-embedding-001 | Free tier |
| Voyage | voyage-4-large | High quality |
| Mistral | mistral-embed | EU-friendly |
| Ollama | nomic-embed-text | Offline |
| Local | embeddinggemma-300m | node-llama-cpp |

Auto-detection: openai → gemini → voyage → mistral → local → FTS-only fallback.

### Pre-Compaction Memory Flush

1. System detects context nearing soft threshold
2. Injects silent user message: "append to `memory/YYYY-MM-DD.md`"
3. Agent writes using file tools; `NO_REPLY` convention (user sees nothing)
4. Compaction runs, summarizing old messages (in-memory only — disk files untouched)
5. Fires once per compaction cycle (tracked via `memoryFlushCompactionCount`)

## 6. Session Persistence

### Two Layers

1. **Session Store** (`sessions.json`) — mutable K/V map: sessionKey → SessionEntry
2. **Transcript** (`<sessionId>.jsonl`) — append-only, tree structure (entries have `id` + `parentId`)

### Session Key Format

- Main/direct: `agent:<agentId>:<mainKey>`
- Group: `agent:<agentId>:<channel>:group:<id>`
- Cron: `cron:<job.id>`
- Webhook: `hook:<uuid>`

### Session Store Entry — Golden Reference

A real session state dump is available at `docs/openclaw_ref/openclaw_state.json` (captured from a live instance). It serves as the golden reference for US-OC-004a (Session State Schema Extension) — see `prd.json` for the annotated story.

### Maintenance

- Prune after: 30 days (default)
- Max entries: 500
- Rotate bytes: 10MB
- Enforcement: prune stale → cap count → archive → purge old archives → rotate → enforce disk budget

## 7. Tool System

### Built-In Tools

Core: `read`, `write`, `edit`, `grep`, `find`, `ls`, `exec`, `process`
Browser: `web_search`, `web_fetch`, `browser`
Sessions: `sessions_list`, `sessions_history`, `sessions_send`, `sessions_spawn`
Memory: `memory_search`, `memory_get`
Other: `canvas`, `nodes`, `cron`, `message`, `image`, `apply_patch`

### Tool Execution

- Sandboxing (optional per agent/session)
- Exec approval (gateway prompt → user approval)
- Tool policy (allowlist/denylist, per-agent + per-session)
- Tool result truncation (cap output size for context budget)

---
