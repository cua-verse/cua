# US-SUB-013: Async DelegateGUITool — Unblock General Subagents

## Context

`DelegateGUITool.call()` blocked the main event loop via `ThreadPoolExecutor + future.result()`. General subagents (`asyncio.Task`s on the main loop) were completely starved during GUI delegations — their coroutines froze because the event loop thread was stuck in a synchronous `future.result()` call.

**Fix:** Make `DelegateGUITool` fire-and-forget like `DelegateGeneralTool` — schedule as an `asyncio.Task`, return `accepted` immediately. Results arrive via the existing `_drain_completions()` + `_drain_post_delegation()` mechanism.

## Changes

### 1. `subagent_registry.py` — Remove GENERAL-only guard in `complete()` and `fail()`

Remove `if run.type == SubagentType.GENERAL:` so both general and GUI runs push to `_completion_queue`.

### 2. `subagent_tools.py` — Rewrite `DelegateGUITool.call()` to fire-and-forget

Replace `ThreadPoolExecutor + asyncio.run` with `loop.create_task()` (same as `DelegateGeneralTool`). `_drive()` becomes a void coroutine. `call()` returns `{"status": "accepted"}` immediately.

### 3. `agent_loop.py` — Log drained messages to transcript

`_drain_completions()` and `_drain_post_delegation()` now call `session_mgr.append_message()` so subagent results appear in the transcript JSONL. Also updated docstrings.

### 4. `AGENTS.md` — Update `delegate_gui` docs

Changed from "blocking, returns summary" to "async, auto-announces" with VM-occupied constraint.

### 5. `test_subagent_drain.py` — Flip GUI queue test, add session_mgr to stub

Renamed `test_gui_completions_do_not_enter_queue` → `test_gui_completions_enter_queue`. Added `session_mgr = MagicMock()` to `_DrainStub`.

### 6. `test_subagent_tools.py` — Rewrite `TestDelegateGUITool` for async pattern

All tests assert `{"status": "accepted"}` + `await _await_gui_task()` + drain completions/post-delegation queues.

## Key design decisions

- **No CUA framework changes.** `BaseTool.call()` stays sync. Uses `asyncio.get_running_loop().create_task()` — same proven pattern as `DelegateGeneralTool`.
- **VM-occupied soft constraint.** Enforced via prompt guidance, not code.
- **Drain ordering preserved.** `_drain_completions()` before `_drain_post_delegation()` — summary text arrives before post-delegation screenshot.

## Verification

1. `pytest tests/test_subagent_gui.py tests/test_subagent_gui_protocol.py tests/test_subagent_tools.py tests/test_subagent_drain.py -v` — all 147 pass
2. VM test confirmed: `delegate_gui` returns `accepted`, model sees `[Subagent Result]` via drain (evidenced by thinking: "the last subagent mentioned reaching floor 1")
