# OpenClaw Subagent Architecture Analysis

Investigation date: 2026-04-10

## Overview

OpenClaw has a full hierarchical subagent system for spawning parallel workers. Subagents are the **only** truly async, independently-running entities — all other "side" LLM calls (compaction, memory flush, image analysis) are synchronous helper calls that block the main loop.

## Key Files

| File | Purpose |
|------|---------|
| `src/agents/subagent-spawn.ts` (847 LOC) | Creates child sessions, validates constraints (depth, concurrency) |
| `src/agents/subagent-registry.ts` | In-memory + disk-backed tracking of all subagent runs |
| `src/agents/subagent-announce.ts` | Push-based result delivery back to parent |
| `src/agents/subagent-capabilities.ts` | Depth-based role resolution |
| `src/agents/tools/sessions-spawn-tool.ts` | `sessions_spawn` tool — entry point for spawning |
| `src/agents/tools/subagents-tool.ts` | `subagents` tool — list/kill/steer actions |
| `src/agents/subagent-control.ts` | Kill, steer, list operations |
| `src/agents/subagent-announce-delivery.ts` | Retry, transient/permanent failure handling |
| `src/agents/subagent-announce-output.ts` | Result extraction and capture |
| `src/agents/subagent-announce-queue.ts` | Batching multiple pending completions with debounce |
| `src/agents/subagent-registry-lifecycle.ts` | Lifecycle state machine |
| `src/agents/subagent-lifecycle-events.ts` | Constants (complete, error, killed, reset, delete) |
| `src/agents/internal-events.ts` | Structured completion event format |

## Delegation Flow

### Phase 1: Spawn

1. Agent calls `sessions_spawn` tool with `task`, optional `label`, `agentId`, `model`
2. System validates: agentId format, spawn depth (max 3), active children count (max 5), sandbox compatibility
3. Creates child session key: `agent:{targetAgentId}:subagent:{randomUUID()}`
4. Patches child session metadata with resolved role, depth, model
5. Builds subagent system prompt (role constraints, push-based completion rules)
6. Calls gateway `agent` RPC with lane `AGENT_LANE_SUBAGENT`
7. Registers run in subagent registry
8. Returns `{ status: "accepted", childSessionKey, runId }`

### Phase 2: Execution

- Subagent runs its task in an isolated session with inherited workspace
- Can spawn its own children (if depth < maxSpawnDepth)
- Results auto-announce back to **its parent**, not the root agent

### Phase 3: Auto-Announce (Result Collection)

1. Subagent finishes → gateway emits lifecycle event (`phase: "end"`)
2. Registry listener catches it → `completeSubagentRun()` freezes result text
3. Announce flow builds structured `AgentInternalEvent` (task_completion type)
4. Delivery via `callGateway({ method: "agent" })` to parent session
5. If parent is streaming: `steer()` injects message mid-turn
6. If parent is busy: queued in `ANNOUNCE_QUEUES`, drained when idle
7. Multiple completions coalesced by debounce + queue summary

### Phase 4: Parent Integration

- Parent receives completion as a formatted user message with structured event block
- Format includes: source, session_key, task label, status, result (in `<<<BEGIN_UNTRUSTED_CHILD_RESULT>>>` tags), action instruction
- Agent synthesizes results and decides: continue orchestrating, spawn more, or send final answer

## Role System

Roles are **purely depth-based**, not chosen by the caller:

```
resolveSubagentRoleForDepth({ depth, maxSpawnDepth }):
  if depth <= 0  → "main"
  if depth < maxSpawnDepth → "orchestrator"  
  if depth >= maxSpawnDepth → "leaf"
```

Default `maxSpawnDepth` = 3:

| Depth | Role | Can Spawn? | Control Scope | Tool Access |
|-------|------|-----------|---------------|-------------|
| 0 | main | Yes | children | Full |
| 1 | orchestrator | Yes | children | Full minus system admin |
| 2 | orchestrator | Yes | children | Full minus system admin |
| 3+ | leaf | No | none | No session tools, no system admin |

Stored role in session store can override depth calculation (for session restoration edge cases).

## Tool Access

### Always denied to all subagents
`gateway`, `agents_list`, `whatsapp_login`, `session_status`, `cron`, `sessions_send`

### Additionally denied to leaf subagents
`subagents`, `sessions_list`, `sessions_history`, `sessions_spawn`

### Available to all subagents (including leaves)
All I/O (read, write, edit), exec, web search/fetch, **browser automation**, canvas/UI, image tools, message, memory, PDF

**No predefined subagent archetypes** (no "explore" agent, "research" agent, etc.). All subagents are generic workers. Specialization comes from the task string, tool allow/deny config, and model selection.

## Helper Calls vs Subagents

| Operation | Mechanism | Blocks Main Loop? | Uses Gateway? | Registry? |
|-----------|-----------|-------------------|---------------|-----------|
| Compaction | `session.compact()` — pi-agent-core built-in | Yes | No | No |
| Memory flush | Direct LLM call within the turn | Yes | No | No |
| Image analysis | Direct VLM call (`image` tool) | Yes | No | No |
| **Subagent** | `sessions_spawn` → gateway → new session | **No** | **Yes** | **Yes** |

Subagents are reserved exclusively for user-initiated parallel work. All infrastructure operations (compaction, memory, vision) are inline helper calls.

## Delivery Mechanism: `steer()`

The core mechanism is message injection via `activeSession.steer(text)`:

- `queueEmbeddedPiMessage()` retrieves the active run handle from `ACTIVE_EMBEDDED_RUNS` map
- Checks: `isStreaming()` must be true, `isCompacting()` must be false
- If conditions met: calls `steer()` which appends message to session and triggers next LLM turn
- If conditions not met: returns false, message is queued for later delivery

This is **push-based event delivery**, not polling. The parent agent does not actively check for subagent completion.

## Wake on Descendant Settle

When a parent finishes its turn while children are still pending:

1. Registry marks `wakeOnDescendantSettle = true`
2. As children complete, registry checks `countPendingDescendantRuns()` → 0
3. Triggers `wakeSubagentRunAfterDescendants()` which injects a wake message:
   - "[Subagent Context] All pending descendants have now settled."
   - Includes merged child results
4. Gateway invokes parent session with wake message → parent resumes

## Configuration

| Constraint | Default | Config Path |
|-----------|---------|-------------|
| Max spawn depth | 3 | `agents.defaults.subagents.maxSpawnDepth` |
| Max active children per agent | 5 | `agents.defaults.subagents.maxChildrenPerAgent` |
| Announce timeout | 90s | `agents.defaults.subagents.announceTimeoutMs` |
| Run timeout | 48h | `agents.defaults.timeoutSeconds` |
| Max announce retries | 100 | hardcoded |
| Announce retry delays | 5s, 10s, 20s | hardcoded |

## Implications for CUA Implementation

### What maps directly
- Registry concept (in-memory tracking + persistence)
- Depth-based role system
- Tool deny lists per role
- Subagent system prompt (focused worker, don't initiate, push-based results)
- Result formatting as structured events

### What doesn't map
- Gateway-mediated session routing → we use asyncio.Task pool instead
- `steer()` mid-stream injection → we drain completions between steps
- Multi-session architecture → shared process, separate asyncio tasks
- Persistent session keys with UUID routing → simple run_id tracking

### Recommended CUA approach
- **Type A (LLM-only)**: asyncio.Task making litellm calls, no VM access. Covers planning, analysis, verification.
- **Type B (Full agent)**: separate OpenClawComputerAgent on its own VM. Heavy lift, future work.
- **Completion delivery**: drain asyncio.Queue between predict_step calls, inject as user messages into items list.
- **Implementation sequence**: US-OC-050-054 (unified loop) first, then subagent stories on the stable loop.
