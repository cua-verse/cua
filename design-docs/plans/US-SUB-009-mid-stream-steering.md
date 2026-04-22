# US-SUB-009 — Subagent Mid-Stream Steering

## Context

The main agent can spawn general subagents via `delegate_general` (US-SUB-005) that run asynchronously with persistent sessions (US-SUB-008). Currently, once spawned, there's no way to refine or redirect a running subagent — the only control is `kill`. US-SUB-009 adds a `steer` action to inject follow-up messages into a running subagent's conversation between its own turns, matching OpenClaw's `steerControlledSubagentRun`.

## OpenClaw Design Rationale

### What OpenClaw Does
- `subagent-control.ts:steerControlledSubagentRun` — heavyweight abort-and-restart: aborts the current embedded PI run via gateway, clears session queues, waits for settle (5s timeout), re-sends via gateway as a new run, replaces the run record with `replaceSubagentRunAfterSteer`.
- `subagents-tool.ts` steer action: validates `MAX_STEER_MESSAGE_CHARS` (4000), resolves target via `resolveControlledSubagentTarget`, rate-limits at 2s.
- `subagents-utils.ts:resolveSubagentTargetFromRuns` — precedence: "last" keyword → numeric index → session key (contains `:`) → exact label → label prefix → run ID prefix.

### What We Keep and Why
- **MAX_STEER_MESSAGE_CHARS = 4000** — reasonable cap preventing token-bomb injections.
- **Target resolution precedence** (simplified): exact run_id → label match (case-insensitive) → run_id prefix → "last" keyword / most-recent-active fallback. Matches the PRD's stated order.
- **Single-message-per-turn guard** — prevents message storms; the subagent processes one steer message per loop iteration.

### What We Drop and Why
- **Abort-and-restart mechanism** — OpenClaw's steer aborts the current PI run via gateway because it's multi-process. We're single-process asyncio; an `asyncio.Queue` polled between turns is sufficient and far simpler.
- **Run record replacement** (`replaceSubagentRunAfterSteer`) — our steer doesn't create a new run; it injects a message into the existing session.
- **Rate limiting** (2s) — unnecessary in our model; the main agent can only steer between its own turns (sequential tool calls), which is naturally rate-limited.
- **Gateway call, session ID lookup, embedded PI abort** — all gateway/multi-process infrastructure we don't have.
- **Numeric index and session-key resolution** — our subagent model is simpler; run_id + label + prefix covers all practical cases.

### Key Differences from OpenClaw
- OpenClaw steer = abort current run + restart with new message. Our steer = inject message into inbox queue polled between turns. Same user-facing behavior (subagent sees the message and can adapt), much simpler internals.
- No new run_id is generated; the run continues with the same ID.
- Inbox is an `asyncio.Queue[str]` created per session, attached to the registry for the tool to find.

---

## Implementation Plan

### 1. SubagentRegistry — inbox management (`subagent_registry.py`)

**Add** `_inboxes: dict[str, asyncio.Queue[str]]` alongside `_tasks`.

**New methods**:
- `attach_inbox(run_id, inbox)` — stores queue; no-op if run_id unknown
- `get_inbox(run_id) -> asyncio.Queue[str] | None` — returns the queue

**Modify terminal transitions** (`complete`, `fail`, `kill`) to pop `self._inboxes.pop(run_id, None)` — prevents steer into a finished run and avoids memory leaks.

### 2. GeneralSubagentSession — inbox polling (`subagent_session.py`)

**Add** `self._inbox: asyncio.Queue[str] = asyncio.Queue()` in `__init__`.

**Expose** `inbox` as a read-only property (so the wrapper can hand it to the registry).

**Add** `_poll_inbox()` method:
```python
def _poll_inbox(self) -> None:
    try:
        msg = self._inbox.get_nowait()
    except asyncio.QueueEmpty:
        return
    self._messages.append({"role": "user", "content": msg})
    self.session_mgr.append_message("user", f"[Steer] {msg}")
```

**Insert call** in `run()` between proactive compaction (step 1) and pre-call token estimate (step 2):
```python
# 1. Proactive compaction...
# 1.5 Poll inbox for steer messages (at most one per turn)
self._poll_inbox()
# 2. Pre-call token estimate...
```

This ensures: steer message is counted in token estimation, included in the API call, and only one message is consumed per turn (additional messages stay queued).

### 3. run_general_subagent wrapper — attach inbox (`subagent_general.py`)

After creating the `GeneralSubagentSession` and before calling `session.run()`, add:
```python
registry.attach_inbox(run_id, session.inbox)
```

This wires the session's inbox to the registry so `SubagentsTool.steer` can find it.

### 4. SubagentsTool — steer action (`subagent_tools.py`)

**Add** `MAX_STEER_MESSAGE_CHARS = 4_000` constant.

**Add** target resolution function `_resolve_steer_target(registry, target_str)`:
1. Exact `run_id` match via `registry.get_run(target)`
2. Exact label match (case-insensitive) among active runs
3. Run ID prefix match among active runs
4. `"last"` keyword → most recently created active run
5. Return `None` if no match

**Extend** `SubagentsTool.parameters`: add `"steer"` to action enum, add `"message"` property.

**Extend** `SubagentsTool.call()` with steer branch:
- Validate `target` and `message` are present
- Enforce `MAX_STEER_MESSAGE_CHARS`
- Resolve target via `_resolve_steer_target`
- Check run is not terminal
- `registry.get_inbox(run_id).put_nowait(message)`
- Return `{"status": "ok", "steered": run_id}`
- Error cases: missing target/message, message too long, unknown target, no inbox (run finished between resolve and steer)

**Update** `SubagentsTool.description` to mention steer.

### 5. Tests

**`tests/test_subagent_registry.py`** — new tests:
- `attach_inbox` + `get_inbox` round-trip
- `get_inbox` returns None for unknown run_id
- Terminal transitions (`complete`/`fail`/`kill`) clean up inbox

**`tests/test_subagent_session.py`** — new tests:
- Inbox message is consumed between turns (mock LLM, put a message in inbox before second call, verify it appears in messages)
- At most one message per turn (put 2 messages, verify only first consumed on next turn, second on the turn after)
- Inbox property is a Queue

**`tests/test_subagent_tools.py`** — new tests:
- Steer happy path: resolve by exact run_id, message injected
- Steer by label match
- Steer by run_id prefix
- Steer "last" target
- Steer missing message → error
- Steer message too long → error
- Steer unknown target → error
- Steer terminal run → error

**`tests/test_subagent_general.py`** — verify wrapper calls `registry.attach_inbox`.

---

## Files Modified

| File | Change |
|------|--------|
| `submodules/.../openclaw/subagent_registry.py` | `_inboxes` dict, `attach_inbox`, `get_inbox`, cleanup in terminal transitions |
| `submodules/.../openclaw/subagent_session.py` | `_inbox` queue, `inbox` property, `_poll_inbox()`, loop insertion |
| `submodules/.../openclaw/subagent_general.py` | `registry.attach_inbox(run_id, session.inbox)` call |
| `submodules/.../openclaw/subagent_tools.py` | `MAX_STEER_MESSAGE_CHARS`, `_resolve_steer_target()`, steer action in `SubagentsTool` |
| `tests/test_subagent_registry.py` | Inbox attach/get/cleanup tests |
| `tests/test_subagent_session.py` | Inbox polling tests |
| `tests/test_subagent_tools.py` | Steer action tests |
| `tests/test_subagent_general.py` | Wrapper attach_inbox test |

## Verification

1. **Lint**: `uv run ruff check submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/subagent_registry.py submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/subagent_session.py submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/subagent_tools.py submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/subagent_general.py`
2. **Unit tests**: `uv run pytest tests/test_subagent_registry.py tests/test_subagent_session.py tests/test_subagent_tools.py tests/test_subagent_general.py tests/test_subagent_drain.py -x -v`
3. **Full suite**: `uv run pytest tests/ -x` (verify no regressions)
4. **Level 2 VM**: `VM_IP=<ip> bash run_magic_tower.sh 50` — if the agent steers a subagent, verify the steered message appears in the subagent's transcript
