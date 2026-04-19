# US-SUB-001–007: Subagent Delegation Architecture

## Problem

The main agent runs a single loop with one model, one context window, and exclusive VM control. This creates limitations:

1. **Context pressure** — long tasks fill the context window; delegating subtasks to a fresh context would be more efficient
2. **Model lock-in** — the main agent uses one model for everything; routine GUI actions could use a cheaper/specialized vision model (GPT-5.4, UI-TARS)
3. **No parallelism** — planning, analysis, and memory synthesis block the main loop even though they don't need VM access

## Design

Two subagent types with different execution models:

### General Subagent (async, no VM)

- **Purpose**: Planning, analysis, memory synthesis, verification
- **Tools**: `analyze_image`, `memory_search`, `memory_get`, `memory_write` — explicitly NO Computer
- **Execution**: `asyncio.Task` running `litellm.acompletion()` loop with function-calling
- **Lifetime**: One-shot — spawn with task, get result, done
- **Result delivery**: Pushed to `asyncio.Queue`, drained between main agent steps as user messages
- **Model**: Defaults to parent's summary_model (cheaper)

### GUI Subagent (blocking, VM relay)

- **Purpose**: Routine GUI automation delegated to a specialized vision model
- **Tools**: None — pure vision-to-action. Receives screenshot, outputs action
- **Execution**: Blocking relay loop within the main agent's tool handler
- **Flow**:
  1. Main agent calls `delegate_gui(instruction="open Settings app", max_steps=10)`
  2. GUI subagent takes screenshot → sends to vision model → gets action
  3. Action executed on VM via `session.click()` / `session.type_text()` / etc.
  4. New screenshot taken, fed back to vision model
  5. Repeat until DONE or max_steps
  6. Summary returned as tool result to main agent
- **Model**: GPT-5.4 (native computer-use) or other vision model with function-calling fallback

### Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│  Main Agent (Claude — reasoning + orchestration)                │
│                                                                  │
│  predict_step → decides what to do                              │
│       │                                                          │
│       ├─► delegate_gui(instruction, max_steps)  [BLOCKING]      │
│       │        │                                                 │
│       │        │  ┌──────────────────────────────────────┐      │
│       │        └─►│  GUI Subagent (GPT-5.4 / UI-TARS)   │      │
│       │           │                                      │      │
│       │           │  screenshot ──► "click (50,750)"  ───┼──►VM │
│       │           │  screenshot ◄── result             ◄─┼──    │
│       │           │  screenshot ──► "type 'settings'"  ──┼──►VM │
│       │           │  screenshot ◄── result             ◄─┼──    │
│       │           │  "DONE: Settings app is open"        │      │
│       │           └──────────────────────────────────────┘      │
│       │                                                          │
│       ├─► delegate_general(task, model)  [ASYNC]                │
│       │        │                                                 │
│       │        │  ┌──────────────────────────────────────┐      │
│       │        └─►│  General Subagent (Haiku/Sonnet)     │      │
│       │           │  asyncio.Task — no VM access         │      │
│       │           │                                      │      │
│       │           │  tools: memory_*, analyze_image      │      │
│       │           │  result → completion queue           │      │
│       │           └──────────────────────────────────────┘      │
│       │                                                          │
│       ├─► _drain_completions()  ← picks up general results     │
│       │                                                          │
│       └─► continues with both results in context                │
└─────────────────────────────────────────────────────────────────┘
```

## Key Design Decisions

| Decision | Choice | Why |
|----------|--------|-----|
| GUI subagent is blocking | Yes | It drives VM actions — main agent can't use VM concurrently |
| General subagent is async | Yes | No VM dependency — can run while main agent acts |
| GUI subagent executes actions directly | Via session.click() etc. | Not via Computer tool — avoids double-wrapping |
| General subagent has no Computer tool | Filtered out even if passed | Prevents accidental VM access from async context |
| No recursive delegation | Subagents can't spawn subagents | Simplicity — depth 1 only for V1 |
| GUI subagent uses native computer_call for GPT-5.4 | Yes | That's the format the model is trained on |
| Completion drain between steps | After _handle_item, before compaction | Matches OpenClaw's auto-announce insertion point |
| Max 3 concurrent general subagents | Configurable default | Prevents runaway API costs |
| Subagent runs start ephemeral | In-memory for V1 (US-SUB-001); disk-backed JSONL added in US-SUB-007 | Start simple, add persistence once the pattern is validated |

## Story Breakdown

### US-SUB-001: Subagent Base Types & Registry (Priority 15)

**File**: `openclaw/subagent_registry.py`

Foundation types shared by both subagent types:
- `SubagentRun` dataclass: run_id, type (general|gui), task, label, model, status, result_text, usage
- `SubagentRegistry`: in-memory dict + asyncio.Queue for completed general subagents
- Lifecycle: register → running → complete/fail/kill
- Concurrency guard: max 3 active general subagents

### US-SUB-002: General Subagent — Async One-Shot Worker (Priority 16)

**File**: `openclaw/subagent_general.py`

Lightweight litellm.acompletion() loop with function-calling:
- Tools: AnalyzeImageTool, MemorySearchTool, MemoryGetTool, MemoryWriteTool
- Multi-turn: call tools, get results, loop until final text response or max_steps (default 5)
- System prompt: focused worker role adapted from OpenClaw's buildSubagentSystemPrompt
- On completion/error: push to registry completion queue

NOT a full OpenClawComputerAgent — no VM, no trajectory, no compaction, no session.

#### Differences from OpenClaw (as built)

Natural self-termination (OpenClaw "Path 1") is implemented 1:1: the LLM ends its turn with no tool calls → loop breaks → `registry.complete()` pushes to the completion queue, which the main loop drains between `predict_step` calls (analogue to OpenClaw's `steer()` push). System prompt rules and registry lifecycle (running → complete/failed) mirror `buildSubagentSystemPrompt` / `completeSubagentRun`.

Deliberate simplifications vs. OpenClaw:

- **Hard `max_steps=5` cap** (subagent_general.py:147). OpenClaw relies on a 48h `timeoutSeconds` instead. Ours is tighter by design — LLM-only subagents shouldn't need many turns; if they do, something is wrong. On exhaustion we return a "(subagent reached max steps without a final response)" sentinel rather than erroring.
- **One-shot, no re-invocation.** OpenClaw can steer/send further messages to a completed subagent session (`sendControlledSubagentMessage`, `steerControlledSubagentRun`). We don't — each task is a one-shot asyncio function with no persistent session to re-enter. If the parent wants more work, it spawns a new subagent.
- **No orchestrator subagents.** `ALLOWED_TOOL_NAMES` excludes `delegate_general` / `delegate_gui` / `subagents` (subagent_general.py:28-42), so a general subagent cannot spawn children. OpenClaw allows nesting up to depth 3 (main → orchestrator → orchestrator → leaf). Our max depth is effectively 1.
- **No Path 2 (explicit kill) — deferred to US-SUB-005.** OpenClaw's `subagents(action=kill)` aborts the in-flight LLM stream via `abortEmbeddedPiRun`, clears queues, marks the registry `killed`, and cascade-kills descendants. We have the registry slot for it (`SubagentRegistry` lifecycle includes kill) but no wiring: `asyncio.Task.cancel()` on the task handle + a corresponding registry transition needs to land with the delegation tool in US-SUB-005. Cascade-kill is moot for us since there are no descendants (see previous point).
- **No session persistence for subagents.** OpenClaw writes child session metadata to the session store (role, depth, model, `abortedLastRun` flag). We hold state only in the in-memory `SubagentRegistry` for the lifetime of the parent agent. Acceptable because subagents are ephemeral by design; revisit only if we add resume-across-restart.

### US-SUB-003: GUI Subagent — Vision-to-Action Relay Protocol (Priority 16)

**File**: `openclaw/subagent_gui_protocol.py`

Pure types and parsing — no LLM calls, no VM interaction:
- `GUIAction` union: Click, Type, Hotkey, Scroll, Drag, Wait, Done
- `parse_gui_response()`: handles OpenAI computer_call (primary), function_call (fallback), text (last resort)
- `execute_gui_action()`: converts GUIAction → DesktopSession method call
- Action validation: bounds checking, non-empty text, etc.

### US-SUB-004: GUI Subagent — Relay Loop (Priority 17)

**File**: `openclaw/subagent_gui.py`

The blocking relay loop:
```
while steps < max_steps:
    screenshot = await session.screenshot()
    messages = build_messages(system_prompt, screenshot, instruction, history)
    response = await litellm.acompletion(model, messages, tools=...)
    action = parse_gui_response(response)
    if isinstance(action, DoneAction):
        return action.summary
    await execute_gui_action(action, session)
    history.append((screenshot, action))
    # Prune history to last 3 screenshots
```

Safety: action dedup (same action 3x → abort), max_steps hard limit.

### US-SUB-005: Delegation Tools & Main Loop Integration (Priority 18)

**Files**: `openclaw/subagent_tools.py` + modifications to `agent_loop.py`, `tools.py`, `AGENTS.md`

Three new BaseTool subclasses:
- `DelegateGeneralTool`: spawns asyncio.Task, returns immediately
- `DelegateGUITool`: calls run_gui_subagent, blocks, returns result
- `SubagentsTool`: list/kill active subagents

Main loop change: `_drain_completions()` after `_handle_item()`, before compaction check.

### US-SUB-006: Cross-Subagent Context (Priority 19)

**Files**: updates to `subagent_general.py`, `subagent_gui.py`, `agent_loop.py`

Three changes:
1. General subagent accepts optional screenshots (base64) in initial context
2. GUI subagent prunes screenshot history to last 3
3. After GUI delegation completes, fresh screenshot injected into main agent's context

## Dependencies

```
                US-SUB-001 (registry + types)
                 /                    \
                ▼                      ▼
    US-SUB-002 (general)      US-SUB-003 (GUI protocol)
              |                        |
              |                        ▼
              |               US-SUB-004 (GUI relay loop)
              |                        |
              └────────┬───────────────┘
                       ▼
              US-SUB-005 (tools + loop integration)
                       │
                       ▼
              US-SUB-006 (context passing)
                       │
                       ▼
              US-SUB-007 (disk-backed persistence)
```

All stories depend on US-OC-050-054 (unified loop) being done first — the main loop must be stable before adding subagent drain points.

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| GPT-5.4 outputs unparseable actions | Medium | Medium | parse_gui_response has 3 fallback levels; retry once with clarification |
| GUI subagent stuck in loop | Medium | Low | Same-action dedup (3x → abort); max_steps hard limit |
| General subagent LLM call slow | Low | Low | Per-step timeout (30s), total timeout (120s) |
| Main agent over-delegates | Low | Low | AGENTS.md guidance + max concurrent limit (3) |
| Async drain timing causes stale results | Low | Low | Drain is non-blocking; result appears on next step (5-15s latency) |

### US-SUB-007: Subagent Registry Persistence — Disk-Backed Runs (Priority 20)

**File**: updates to `openclaw/subagent_registry.py`

Closes the ephemeral→persistent gap, matching OpenClaw's `subagent-registry-state.ts`:

1. **Persist** — append-only JSONL at `tasks/{task_id}/subagent-runs.jsonl`. Write on every state transition (register, complete, fail, kill). Append-only avoids rewriting the full file.
2. **Restore** — on session resume, load prior runs. Mark pending/running runs from prior sessions as orphaned (`status=error, result_text='stalled: prior session ended before completion'`). Prevents the agent from waiting for results that will never arrive.
3. **History query** — `completed_runs()` exposes prior subagent results for cross-run context (e.g., "in a previous session, you delegated X and the result was Y"). Enables transcript replay to include delegation history.

## Future Extensions (Not in this PRD)

- **VM Lock for true concurrency**: asyncio.Lock wrapping DesktopSession for concurrent GUI + general subagents
- **Multi-VM Type B**: subagent gets its own VM via separate RemoteDesktopSession
- **Recursive delegation**: orchestrator subagents that can spawn their own workers (depth > 1)
- **Subagent model routing**: automatic model selection based on task type
