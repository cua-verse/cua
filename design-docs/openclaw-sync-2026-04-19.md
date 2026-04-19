# openclaw sync review — 2026-04-19

## Context

Upstream openclaw was fast-forwarded by **8,918 commits** (`e133924047` → `f38a498985`, latest tag `v2026.4.19-beta.2`). Agenthle maintains a Python port of openclaw's agent harness under `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/`. This document classifies the substantive upstream changes against the port and produces an actionable backlog.

**Raw artifacts**: `logs/openclaw-sync-2026-04-19/` (per-topic diffs, commit logs, tree delta, rename log). **Safety tag**: `pre-sync-2026-04-19` → `e133924047`. Rollback: `git -C openclaw reset --hard pre-sync-2026-04-19`.

**Classification legend**: MIRROR (port must change to match), ADAPT (deliberate divergence), IGNORE (test/TS-only or not ported), NEW-UPSTREAM (new capability, consider porting).

---

## 1. Compaction

**Upstream**: `src/agents/compaction*.ts`, `src/agents/pi-hooks/compaction-safeguard*.ts`
**Mirror**: `context.py`, `agent_loop.py`

- **[MIRROR]** Removed "tokens" and "API keys" from identifier-preservation guidance.
  - Upstream: `src/agents/compaction.ts:564` (`IDENTIFIER_PRESERVATION_INSTRUCTIONS`)
  - Port: `context.py:386–390`
  - Reason: upstream now excludes these terms; port string still lists them.
- **[MIRROR]** Tool-call/result pairing preserved across token-share splits.
  - Upstream: `src/agents/compaction.ts:573–637` (new `splitCurrentAtPendingBoundary`)
  - Port: `context.py:719–758` (`chunk_messages_by_token_share`) — no pairing logic.
- **[MIRROR]** `resolveContextWindowTokens` falls back to `model.contextTokens`.
  - Upstream: `src/agents/compaction.ts:689–694`
  - Port: `context.py:78–84` (`resolve_context_window`) — no fallback.
- **[MIRROR]** Summarization retry now skips timeout errors.
  - Upstream: `src/agents/compaction.ts:644` (`shouldRetry` excludes `isTimeoutError`)
  - Port: `context.py:906–927` — blanket catch-all retry.
- **[MIRROR]** Fallback summarization deduplicates when no oversized messages found.
  - Upstream: `src/agents/compaction.ts:669`
  - Port: `context.py:998–1012` — no dedup guard.
- **[NEW-UPSTREAM]** `src/agents/pi-compaction-constants.ts` (`MIN_PROMPT_BUDGET_TOKENS`, `MIN_PROMPT_BUDGET_RATIO`).
- **[NEW-UPSTREAM]** Provider-pluggable summarization: `tryProviderSummarize`, `getCompactionProvider`.
  - Upstream: `src/agents/pi-hooks/compaction-safeguard.ts:900–930`
- **[NEW-UPSTREAM]** Split-turn context section (`assembleSuffix`, `formatSplitTurnContextSection`).
  - Upstream: `src/agents/pi-hooks/compaction-safeguard.ts:968–987, 1078–1087`
- **[IGNORE]** Test mock API updates, `gpt-5.2 → gpt-5.4` fixture bumps, test-helper refactors.

## 2. PI-embedded-runner compaction orchestration

**Upstream**: `src/agents/pi-embedded-runner/compact.ts`, `compaction-*.ts`, `manual-compaction-boundary.ts`, `run/preemptive-compaction*`
**Mirror**: `agent_loop.py`, `context.py`

- **[NEW-UPSTREAM]** `resolveModelAuth` extracted to dedicated async helper.
  - Upstream: `src/agents/pi-hooks/compaction-safeguard.ts:1000–1042`
  - Port-equivalent: inline in `agent_loop.py:409–446`. Refactor-for-clarity only.
- **[MIRROR]** Identifier normalization uses `localeLowercasePreservingWhitespace` instead of raw `toLowerCase`.
  - Upstream: `src/agents/pi-hooks/compaction-safeguard-quality.ts:729`
  - Port: `context.py:727–732` (`tokenizeAskOverlapText`) uses plain `.lower()`. Verify whitespace-preserving behavior matches.
- **[IGNORE]** Extensive test additions for provider + safeguard quality paths.

## 3. Tool-result truncation

**Upstream**: `src/agents/pi-embedded-runner/tool-result-truncation.ts`
**Mirror**: `context.py`

- **[MIRROR — CRITICAL]** Hard cap for *live* tool results is now **16,000 chars**, not 400,000. Upstream distinguishes live (per-request) vs. historical (aggregate compaction) budgets.
  - Upstream: `tool-result-truncation.ts:34, 39–40` (`DEFAULT_MAX_LIVE_TOOL_RESULT_CHARS = 16_000`; `HARD_MAX_TOOL_RESULT_CHARS` is now an alias)
  - Port: `context.py:57–58` still uses a single `HARD_MAX_TOOL_RESULT_CHARS = 400_000`.
  - Risk: agenthle currently ships 25× more tool-result text into live turns than upstream expects — could blow past provider caps and degrade prompt-cache hit rates.
- **[MIRROR]** Truncation suffix is now dynamic (reports actual truncated char count).
  - Upstream: `tool-result-truncation.ts:64–87` (`DEFAULT_SUFFIX` factory, `appendBoundedTruncationSuffix`)
  - Port: `context.py:63–67` uses a static string.
- **[NEW-UPSTREAM]** `calculateMaxToolResultCharsWithCap`, `resolveLiveToolResultMaxChars` — per-agent config overrides.
  - Upstream: `tool-result-truncation.ts:186–206`
  - Port: `context.py:194–198` (`_calculate_max_tool_result_chars`) has no config-aware override.
- **[NEW-UPSTREAM]** Aggregate reduction across multiple tool results (`estimateToolResultReductionPotential`, `buildAggregateToolResultReplacements`).
  - Upstream: `tool-result-truncation.ts:378–663`. Port has only per-message truncation.
- **[MIRROR]** `isOversizedToolResult` signature extended with `maxCharsOverride`.
  - Upstream: `tool-result-truncation.ts:774–787`
  - Port: `context.py:375` lacks the optional override.

## 4. Session-transcript repair

**Upstream**: `src/agents/session-transcript-repair*.ts`
**Mirror**: `context.py` (`repair_tool_use_result_pairing`)

- **[MIRROR]** Attachment redaction now preserves `name`, `mimeType`, `encoding` alongside redacted content.
  - Upstream: `session-transcript-repair.ts:432–447` (`redactSessionsSpawnAttachment`)
  - Port: `context.py:511–520` discards metadata, keeps only `{content: '__OPENCLAW_REDACTED__'}`.
- **[MIRROR]** `preserveErroredAssistantResults` (boolean) replaced by `erroredAssistantResultPolicy` (union `"preserve" | "drop"`).
  - Upstream: `session-transcript-repair.ts:545–549`
  - Port: `context.py:511–520` still uses the boolean.
- **[NEW-UPSTREAM]** Shared tool-call constants module (`SESSIONS_SPAWN_ATTACHMENT_METADATA_KEYS`, `normalizeAllowedToolNames`, `isAllowedToolCallName`).
  - Upstream: `session-transcript-repair.ts:1, 338–347, 342–347` (imports from `tool-call-shared.js`)
  - Port has this logic inline; extraction is optional cleanup.
- **[NEW-UPSTREAM]** Signed thinking-block replay safety (`isReplaySafeThinkingAssistantTurn`, `allowProviderOwnedThinkingReplay`).
  - Upstream: `session-transcript-repair.ts:476–502`. Feature-gated; no port yet.
- **[IGNORE]** New tests for attachment redaction, thinking blocks, orphaned results.

## 5. Memory (flush + search)

**Upstream**: `src/auto-reply/reply/agent-runner-memory*.ts`, `src/agents/memory-search*.ts`
**Mirror**: `memory_flush.py`, `memory.py`, `agent_loop.py`

- **[MIRROR]** `ReplyOperation` parameter threaded through memory-flush orchestration, with lifecycle hooks: `setPhase`, `abortSignal`, `updateSessionId`.
  - Upstream: `agent-runner-memory.ts:355, 514, 837, 864, 897, 941, 960, 988`
  - Port: `memory_flush.py` / `agent_loop.py` have no ReplyOperation equivalent. Biggest structural change in this topic.
- **[MIRROR]** New sender metadata fields piped into `compactEmbeddedPiSession`: `senderId`, `senderName`, `senderUsername`, `senderE164`.
  - Upstream: `agent-runner-memory.ts:853–856`. Port: `memory_flush.py` does not forward these.
- **[MIRROR]** `readPostCompactionContext` signature changed to `{ cfg, agentId }`.
  - Upstream: `agent-runner-memory.ts:794–797`. Port must add `agent_id` argument.
- **[MIRROR]** `resolveMemoryFlushContextWindowTokens` now accepts `cfg` + `provider` for provider-aware token counting.
  - Upstream: `agent-runner-memory.ts:828–830, 888–889`.
- **[NEW-UPSTREAM]** `resolveMemorySearchSyncConfig` exported getter + `ResolvedMemorySearchSyncConfig` type.
  - Upstream: `memory-search.ts:234, 358–369`. Relevant if port queries sync state.
- **[IGNORE]** TS lazy-loading of `pi-embedded.js` (`loadPiEmbeddedRuntime`, `setAgentRunnerMemoryTestDeps`); `normalizeOptionalString` plumbing.

## 6. System prompt

**Upstream**: `src/agents/system-prompt*.ts`, `src/agents/pi-embedded-runner/system-prompt.ts`
**Mirror**: `prompt.py`

- **[NEW-UPSTREAM]** Prompt-cache boundary infrastructure (`SYSTEM_PROMPT_CACHE_BOUNDARY`, `splitSystemPromptCacheBoundary`, `stripSystemPromptCacheBoundary`, `prependSystemPromptAdditionAfterCacheBoundary`).
  - Upstream: `src/agents/system-prompt-cache-boundary.ts`
  - Port: none. Worth porting if agenthle uses Anthropic prompt caching (it does, via the Claude SDK).
- **[NEW-UPSTREAM]** `ProviderSystemPromptContribution` type (stable prefix / dynamic suffix / section overrides).
  - Upstream: `src/agents/system-prompt-contribution.ts`
- **[NEW-UPSTREAM]** Per-agent `resolveSystemPromptOverride`.
  - Upstream: `src/agents/system-prompt-override.ts`
- **[MIRROR]** Embedded system-prompt builder signature: new `canvasRootDir`, `includeMemorySection`, `promptContribution`; inline `buildToolSummaryMap` removed.
  - Upstream: `pi-embedded-runner/system-prompt.ts:43–88`
- **[MIRROR]** Session system-prompt mutation changed from `session.agent.setSystemPrompt(prompt)` to `session.agent.state.systemPrompt = prompt`.
  - Upstream: `pi-embedded-runner/system-prompt.ts:103`. Verify Python Pi-session API equivalent.

## 7. PI-tools

**Upstream**: `src/agents/pi-tools*.ts`
**Mirror**: `tools.py`

- **[MIRROR]** Parameter validation simplified: `REQUIRED_PARAM_GROUPS` now supports a `validator` field; `getToolParamsRecord` + updated `assertRequiredParams` replace alias-normalization path.
  - Upstream: `pi-tools.read.ts` (new helpers)
  - Port: `tools.py` param validation should pick up the validator field.
- **[IGNORE]** Claude-alias schema machinery removed (`CLAUDE_PARAM_ALIASES`, `normalizeClaudeParamAliases`, `patchToolSchemaForClaudeCompatibility`). Port never carried this; no action.
- **[IGNORE]** Exec host defaults + group policy test reorganization (`pi-tools-agent-config.exec.test.ts`, policy helper extraction). Behavioral tests; underlying contract unchanged.
- **[IGNORE]** Feishu/topic session-key policy test coverage.

## 8. Subagent protocol

**Upstream**: `src/agents/subagent-announce*`, `src/agents/subagent-registry*`
**Mirror**: `subagent_registry.py`, `subagent_general.py`, `subagent_gui.py`, `subagent_gui_protocol.py`
**Note**: agenthle *extends* the upstream subagent protocol (US-SUB-* stories). Changes here are highest-risk for silent behavioral drift.

- **[ADAPT]** Announce delivery timeout raised 90s → 120s.
  - Upstream: `subagent-announce-delivery.ts:66` (`DEFAULT_SUBAGENT_ANNOUNCE_TIMEOUT_MS`)
  - Port: verify agenthle's async timeout aligns.
- **[ADAPT]** `DeliveryContext` separated into its own module; new shape `{channel?, to?, accountId?, threadId?}`. Legacy `lastChannel/lastTo/lastThreadId` fields are for SQLite session-store compat only — does not affect agenthle (in-memory).
  - Upstream: `src/agents/subagent-announce-origin.ts` (new file; `resolveAnnounceOrigin`)
- **[MIRROR]** Completion-announce deduplication (CHANGELOG: "deduplicate delivered completion announces so retry or re-entry cleanup does not inject duplicate internal-context completion turns").
  - Port: `SubagentRegistry.complete()` — add run-id idempotency check.
- **[IGNORE]** Registry steer-driven runtime replacement, plugin runtime binding (`replaceSubagentRunAfterSteer`, `configureSubagentRegistrySteerRuntime`, `ensureRuntimePluginsLoaded`). Out of scope for agenthle V1 (no recursive steer, depth-1 subagents).
- **[IGNORE]** `SubagentRegistryDeps` + `__testing.setDepsForTest` injection pattern. Agenthle uses simple in-memory registry.
- **[NEW-UPSTREAM]** Extensive new subagent modules (18 files): `subagent-announce-capture`, `subagent-announce-delivery.runtime`, `subagent-announce.registry.runtime`, `subagent-announce.runtime`, `subagent-control.runtime`, `subagent-registry-announce-read`, `subagent-registry-lifecycle.test`, `subagent-registry-steer-runtime`, `subagent-requester-store-key`, `subagent-session-key`, `subagent-session-metrics`, `subagent-spawn.runtime`, `subagent-spawn.types`, `subagent-system-prompt`, `subagent-list`.
  - Highest-value to mirror: `subagent-spawn.types.ts` (spawn contract shape), `subagent-system-prompt.ts` (subagent prompt builder — currently inlined in agenthle).

## 9. Framework-level meta (AGENTS.md + CHANGELOG highlights)

Agent-framework-relevant highlights (channel/UI/CI entries excluded):

- **[MIRROR]** Prompt-cache stability: volatile inbound chat IDs stripped from system prompt → task-scoped caches can be reused. Agenthle subagents should keep run/session IDs out of prompt-constant sections.
- **[MIRROR]** Context-token usage tracking now records context-only counts (not full request totals).
- **[MIRROR]** Bundled MCP/LSP tools now flow through owner-only + tool-policy pipeline.
- **[MIRROR]** Compaction reserve-token floor capped to model context window (prevents overflow loops on small-context models).
- **[MIRROR]** `prompt_cache_key` support for OpenAI-compatible proxies + compat flag.
- **[MIRROR]** Gateway announces dedup exact + streamed `ANNOUNCE_SKIP`/`REPLY_SKIP` control tokens — strip from user-facing text if used.
- **[MIRROR]** Skills snapshot invalidation on config writes touching `skills.*` — invalidate cached subagent tool lists.
- **[MIRROR]** Config contract: legacy keys removed from public surfaces; backward-compat only via migration/doctor seams.
- **[ADAPT]** Default memory storage mode: `inline` → `separate` (dreaming blocks in `memory/dreaming/{phase}/YYYY-MM-DD.md`).
- **[ADAPT]** Startup/skills prompt budgets trimmed; `memory_get` excerpts capped with continuation metadata.
- **[ADAPT]** Orphaned active-turn user text carried into next prompt before transcript reorder.

## 10. NEW-UPSTREAM scan (beyond topic buckets above)

Also appeared in `src/agents/` and not yet represented in the port. Grouped by theme:

- **Framework core (HIGH interest)**: `AGENTS.md`, `anthropic-payload-policy.ts`, `anthropic-transport-stream.ts`, `bootstrap-mode.ts`, `bootstrap-prompt.ts`, `prompt-cache-stability.ts`, `prompt-cache-retention.ts`, `internal-runtime-context.ts`, `heartbeat-system-prompt.ts`.
- **Model / provider**: `model-selection-*.ts` (×6), `model-ref-shared.ts`, `provider-transport-*.ts`, `provider-request-config.ts`, `openai-*.ts` (×6), `google-thinking-compat.ts`.
- **Tools / execution**: `tools/owner-only-tools.ts`, `tools/update-plan-tool.ts`, `tools/music-generate-*.ts`, `tools/video-generate-*.ts`, `task-*.ts`.
- **Pi-embedded runner modularization**: `pi-embedded-runner/run/*.ts` (30+ files: assistant failover, prompt helpers, auth, tool-call normalization, incomplete-turn handling, preemptive compaction, stream wrapper), `anthropic-cache-*.ts`, `effective-tool-policy.ts`, `google-prompt-cache.ts`.
- **Deletions to note**: ~35 dead provider modules moved to plugins/deprecated (byteplus, chutes, deepseek, doubao, kilocode, opencode-zen, venice, volc-shared, moonshot-provider-compat, …). Agenthle does not track these.

---

## Port revision backlog

Ordered by risk × effort. Each item is keyed by mirror file with cited upstream driver.

### Priority 0 — Critical behavioral drift

1. **Reduce live tool-result cap 400 000 → 16 000 chars** — `context.py:57` ↠ upstream `tool-result-truncation.ts:34`. Also add `DEFAULT_MAX_LIVE_TOOL_RESULT_CHARS` / `HARD_MAX_TOOL_RESULT_CHARS` distinction.
2. **Thread `ReplyOperation` (or a Python equivalent) through memory flush** with `set_phase` / `abort_signal` / `update_session_id` hooks — `memory_flush.py`, `agent_loop.py` ↠ upstream `agent-runner-memory.ts:355, 514, 837, 864, 897, 941, 960, 988`.

### Priority 1 — Faithful-port fixes

3. Remove "tokens" and "API keys" from `IDENTIFIER_PRESERVATION_INSTRUCTIONS` — `context.py:386`.
4. Preserve assistant tool-call ↔ tool-result pairing in `chunk_messages_by_token_share` — `context.py:719–758`.
5. Add `model.contextTokens` fallback in `resolve_context_window` — `context.py:78`.
6. Skip timeout errors in summarization retry — `context.py:906–927`.
7. Dedup fallback summarization when no oversized messages — `context.py:998–1012`.
8. Dynamic truncation suffix reporting exact char count — `context.py:154–191`.
9. Config-aware tool-result cap (`calculateMaxToolResultCharsWithCap`) — `context.py:194–198`.
10. Preserve attachment metadata (name, mimeType, encoding) during redaction — `context.py:511–522`.
11. Rename `preserveErroredAssistantResults` (bool) → `errored_assistant_result_policy` (enum `"preserve" | "drop"`) — `context.py:511–520`.
12. Forward sender metadata (`sender_id`, `sender_name`, `sender_username`, `sender_e164`) to embedded session — `memory_flush.py` ↠ upstream `agent-runner-memory.ts:853–856`.
13. Add `agent_id` param to `read_post_compaction_context` — ↠ upstream `agent-runner-memory.ts:794–797`.
14. Thread `provider` into `resolve_memory_flush_context_window_tokens` — ↠ upstream `agent-runner-memory.ts:828–830`.
15. Embedded system-prompt builder gains `canvas_root_dir`, `include_memory_section`, `prompt_contribution` — `prompt.py` ↠ upstream `pi-embedded-runner/system-prompt.ts:43–88`.
16. Align `REQUIRED_PARAM_GROUPS` / `assert_required_params` with validator field — `tools.py`.
17. Subagent completion-announce idempotency (run-id dedup) — `subagent_registry.py`.

### Priority 2 — New capabilities worth porting

18. Prompt-cache boundary helpers (`SYSTEM_PROMPT_CACHE_BOUNDARY`, split/strip/prepend) — new `prompt_cache_boundary.py` ↠ upstream `system-prompt-cache-boundary.ts`.
19. Per-agent system-prompt override (`resolve_system_prompt_override`) — `prompt.py` ↠ upstream `system-prompt-override.ts`.
20. Extract `_build_subagent_system_prompt` into its own module mirroring upstream `subagent-system-prompt.ts` — improves future sync ergonomics.
21. Mirror `subagent-spawn.types.ts` shape as a Python dataclass — spawn contract stability.
22. Adopt `subagent-announce-origin` delivery-context pattern if/when agenthle gains channel routing — track `thread_id`.

### Priority 3 — Discretionary / feature-gated

23. Pluggable compaction provider (`tryProviderSummarize`) — optional, only if agenthle wants provider-owned summarization.
24. Aggregate tool-result reduction (`estimateToolResultReductionPotential`) — optional; current per-message truncation is usually sufficient once Priority 0 #1 lands.
25. Signed thinking-block replay safety — feature-gated; defer until agenthle consumes Claude thinking blocks in replays.
26. Subagent session metrics (`subagent-session-metrics.ts`) — observability, not correctness.
27. Import `pi-compaction-constants` values (`MIN_PROMPT_BUDGET_TOKENS`, `MIN_PROMPT_BUDGET_RATIO`) into `context.py` for parity.

### Out of scope (noted, not backlogged)

- Lazy-load TS runtime patterns (`loadPiEmbeddedRuntime`, `.runtime.ts` modules) — Python imports are statically resolved.
- Claude-param alias normalization — deliberate divergence; agenthle never carried this.
- Subagent steer-driven registry replacement, plugin-runtime binding — depth > 1 subagents are post-V1.
- Bootstrap mode, heartbeat system prompt — framework features not used by agenthle.
- Deleted provider modules (byteplus, chutes, etc.) — never ported.

---

## Verification

- Every Priority 0 and Priority 1 item cites a specific upstream file:line (or function) AND a Python mirror location.
- Raw diffs supporting each claim live under `logs/openclaw-sync-2026-04-19/per-topic/`.
- The "IGNORE" set is explicit so a future reviewer does not re-investigate these during the next sync.

## Next step

Materialize the backlog as tracked work (Step 6 of the sync plan): append a dated section to `prd.json` or `progress.txt` and populate the session task list. Port code changes are a **separate** execution — not part of this sync.
