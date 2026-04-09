# PiAgent Control Loop — Gap Analysis

<!-- Last updated: 2026-04-09 -->

Gap analysis comparing OpenClaw's PiAgent embedded runner (`openclaw/src/agents/pi-embedded-runner/`) against the AgentHLE agent loop (`openclaw_agent.py` + `agent_loop.py`).

## Architecture Comparison

| Aspect | PiAgent (OpenClaw) | AgentHLE (Current) |
|--------|-------------------|-------------------|
| Loop structure | 3-layer: Run (retry) → Attempt (single turn) → Subscribe (events) | 2-layer: `perform_task()` → `OpenClawComputerAgent.run()` |
| Compaction model | Dual: in-place auto-compaction (framework) + explicit recovery (run-level) | Single: in-place only via `_compact_in_place()` |
| Error recovery | Profile rotation → thinking fallback → model failover → tool truncation | None — single API failure is terminal |
| Event model | Subscribe/emit with lifecycle hooks | Procedural with CUA callbacks |
| Memory flush | Post-turn via tool-result-guard + idle detection | Pre-API via `_maybe_flush_memory()` |

## Identified Gaps

### Gap 1: Run-Level Retry Loop (HIGH)

**PiAgent**: `run.ts` wraps every attempt in `while(true)` with escalating recovery:
1. In-place auto-compaction (framework handles)
2. Explicit overflow compaction (max 3 attempts)
3. Timeout compaction (max 2 attempts)
4. Tool result truncation fallback (`truncateOversizedToolResultsInSession()`)
5. Max run loop iteration limit

**AgentHLE**: Single attempt. If `_compact_in_place()` doesn't free enough space and the next API call overflows again, we hit `max_compactions` (default 3) and the agent stops.

**Impact**: Long-running tasks (500+ steps) with multiple compaction cycles can hit cascading overflow with no recovery path.

**Existing PRD stories**: None directly. US-OC-016 (Multi-Stage Summarization) addresses a related scaling issue but from the summarization side, not the retry/recovery side.

**Recommendation**: New story — "Run-Level Retry with Escalating Recovery" covering overflow retry, post-failure tool truncation, and iteration limits.

---

### Gap 2: Auth Profile Rotation / Model Failover (MEDIUM)

**PiAgent**: Maintains ordered list of auth profile candidates. On rate-limit, timeout, billing, or auth errors:
1. Mark current profile as failed
2. Advance to next profile candidate
3. If all profiles exhausted, throw `FailoverError` for outer model fallback handler

**AgentHLE**: Single model, single API key. Any transient API error (rate limit, timeout) terminates the run.

**Impact**: Benchmark runs are fragile — a single rate-limit response kills a 200-step run.

**Existing PRD stories**: None. US-OC-010 (Category A Skipped Component Review) mentions "model failover" as a higher-risk skip to verify, but no implementation story exists.

**Recommendation**: New story — "API Error Recovery: Retry + Model Failover" with a simple 2-model fallback list and exponential backoff on transient errors.

---

### Gap 3: Thinking Level Fallback (LOW)

**PiAgent**: `pickFallbackThinkingLevel()` parses API error messages, downgrades thinking along the chain: xhigh → high → medium → low → minimal → off, then retries.

**AgentHLE**: Crashes if the API rejects the requested thinking level.

**Existing PRD stories**: **US-OC-042** (Thinking Level Fallback on API Rejection) — already filed, exact match. Priority 36.

**Recommendation**: Implement US-OC-042 as-is. No new story needed.

---

### Gap 4: Post-Failure Tool Result Truncation (LOW-MEDIUM)

**PiAgent**: When compaction alone doesn't fix overflow, falls back to `truncateOversizedToolResultsInSession()` — scans persisted session for oversized tool results and truncates them.

**AgentHLE**: `truncate_tool_results()` runs proactively in `on_llm_start` but has no post-failure reactive path. If proactive truncation + compaction still overflows, there's no further recovery.

**Existing PRD stories**: None directly. This is part of Gap 1 (run-level retry).

**Recommendation**: Bundle with the run-level retry story as an escalation step.

---

### Gap 5: Compaction Wait / Framework Auto-Retry (ARCHITECTURAL — LOW)

**PiAgent**: The pi-coding-agent SDK can auto-compact and auto-retry the LLM call. The subscribe layer tracks this via `compactionRetryPromise`, and the attempt waits for it with `waitForCompactionRetryWithAggregateTimeout()` (60s aggregate timeout).

**AgentHLE**: Compaction is fully self-managed in `OpenClawComputerAgent.run()`. No framework auto-compaction exists because we control the loop directly.

**Existing PRD stories**: None needed — this is a CUA SDK architectural difference. Our in-place compaction achieves the same outcome through a different mechanism.

**Recommendation**: No action. Our approach is simpler and equivalent for our use case.

---

### Gap 6: Event-Driven Lifecycle Hooks (ARCHITECTURAL — LOW)

**PiAgent**: Rich event model with `before_compaction`, `after_compaction`, `llm_input`, `llm_output`, `agent_start`, `agent_end` hooks. Enables plugin-style extension.

**AgentHLE**: Procedural loop with CUA AsyncCallbackHandler hooks (`on_llm_start`, `on_function_call_start/end`). Less extensible but sufficient.

**Existing PRD stories**: None needed unless extensibility becomes a requirement.

**Recommendation**: No action for now. If we need plugin-style hooks later, file a story then.

---

## PRD Story Mapping

### Stories that directly close gaps

| Gap | Existing Story | Status | Notes |
|-----|---------------|--------|-------|
| Gap 3: Thinking fallback | **US-OC-042** | Not passed | Exact match — implement as-is |

### Stories that partially overlap with gaps

| Gap | Existing Story | Status | Overlap |
|-----|---------------|--------|---------|
| Gap 1: Retry loop | US-OC-016 (Multi-Stage Summarization) | Not passed | Addresses summarization scaling but not retry/recovery |
| Gap 2: Model failover | US-OC-010 (Category A Review) | Not passed | Mentions model failover as a skip to verify, not implement |
| Gap 1: Retry loop | US-OC-032 (Cross-Model Format Compat) | Not passed | Format compatibility needed for failover but doesn't implement failover itself |

### Stories unrelated to loop gaps but still pending

| Story | Category |
|-------|----------|
| US-OC-023 | Anthropic loop compatibility (provider-specific) |
| US-OC-049 | Transcript observability (metadata) |
| US-OC-020 | Thinking cleanup (hygiene) |
| US-OC-009 | Category C SDK audit (review) |
| US-OC-029 | Subagent tool (new capability) |
| US-OC-030 | Visual analysis tool (new capability) |
| US-OC-031 | Tool audit (review) |
| US-OC-035 | Milestone verification gate (eval) |
| US-OC-043 | Tool call ID sanitization (format) |
| US-OC-044 | OpenAI dual-ID format (format) |
| US-OC-045 | Write-time tool result guard (persistence) |

### New stories needed

| Gap | Proposed Story | Priority Suggestion |
|-----|---------------|-------------------|
| Gap 1 + Gap 4 | **Run-Level Retry with Escalating Recovery**: Wrap agent loop in retry harness — overflow compaction retry (max 3), post-failure tool truncation fallback, iteration limit. | P20 (before US-OC-016) |
| Gap 2 | **API Error Recovery: Retry + Model Failover**: Catch transient API errors (rate limit, timeout, auth), retry with backoff, fall back to alternate model from a 2-model config list. | P21 |

## Summary

- **15 stories** remain not-passed in the PRD
- **1 story** (US-OC-042) directly closes a gap — implement as-is
- **2 new stories** needed for the two high/medium gaps (retry loop + model failover)
- **12 stories** are unrelated to loop gaps (tools, format, audits, eval)
- **2 gaps** (compaction wait, lifecycle hooks) need no action — architectural differences that are acceptable
