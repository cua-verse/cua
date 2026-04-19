# US-SUB-005 (Revised) + New Stories — Delegation on a Persistent Engine

## Context

Per discussion: before shipping US-SUB-005, we want the **general subagent** to be a persistent session (with its own compaction, its own transcript, configurable lifetime) rather than the current 5-step one-shot function. US-SUB-005 will then wire delegation tools **on top of that real engine**, not on top of a placeholder. This mirrors OpenClaw's architecture where every subagent is a full `pi-embedded-runner` session sharing the main agent's compaction pipeline (see `openclaw/src/agents/pi-embedded-runner/compact.ts`).

This plan file covers the **restructured arc**:

1. **NEW — US-SUB-008**: General Subagent Persistent Session Engine (lands before US-SUB-005)
2. **REVISED — US-SUB-005**: Delegation Tools & Main Loop Integration (depends on US-SUB-008)
3. **NEW — US-SUB-009**: Mid-Stream Steering (deferred, depends on US-SUB-005 + US-SUB-008)
4. **NOTE — US-SUB-007**: scope extension (transcript persistence alongside registry metadata)

Execution order: **US-SUB-008 → US-SUB-005 → US-SUB-006 → US-SUB-007 → US-SUB-009**.

---

## US-SUB-008 — General Subagent Persistent Session Engine (NEW)

### Goal
Replace the one-shot `run_general_subagent()` with a session-backed engine that has its own `SessionManager`, its own `ContextOverflowCallback`, and can compact its history without VM access. The LLM-only subagent becomes a first-class session that can legitimately run for many turns.

### OpenClaw Reference
- `pi-embedded-runner/compact.ts:688-691` — same compaction pipeline for subagents, only the system prompt mode switches to `"minimal"` when `isSubagentSessionKey(sessionKey)` is true.
- `pi-embedded-runner/run/attempt.ts` — the shared agent run loop that both main and subagent sessions execute.

What we keep:
- Unified compaction engine (reuse `compact_messages()` from `openclaw/context.py` — already VM-agnostic).
- Per-session overflow detection + token budget.
- Subagent-scoped transcript directory.

What we drop:
- Gateway streaming, `subscribeEmbeddedPiSession`, cross-process routing, `previous_response_id` chaining (no Responses API WS session in our path).
- Multi-provider auth / bedrock wrappers / MCP runtime.
- `promptMode` switch (our subagent prompt is already "minimal" by design).

### Design

**New file**: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/subagent_session.py`

```python
class GeneralSubagentSession:
    """Persistent LLM-only session for a general subagent run.

    Mirrors the core loop shape of OpenClawComputerAgent but without Computer
    tools or Responses API machinery. Owns its own SessionManager, transcript,
    overflow callback, and compaction counter.
    """

    # Public attributes (read by the wrapper + tests)
    usage: SubagentUsage                  # accumulated across all LLM calls; consumed by run_general_subagent for registry.complete/fail
    session_mgr: SessionManager           # subagent-scoped transcript
    overflow_cb: ContextOverflowCallback  # per-session token budget
    compaction_count: int                 # incremented by _compact_in_place

    def __init__(
        self,
        *,
        run_id: str,
        task: str,
        model: str,
        tools: list,                          # full tool list; will be filtered
        registry: SubagentRegistry,
        memory_store: MemoryStore,
        summary_model: str,
        parent_session_dir: Path,             # main agent's session dir
        max_steps: int = 50,                  # safety rail, NOT context cap
        max_compactions: int = 3,
        thinking_params: dict | None = None,
        summary_runtime: ResolvedModel | None = None,
    ):
        self.usage = SubagentUsage()

        # Subagent-scoped session: <parent_session_dir>/subagents/<run_id>/
        self.session_mgr = SessionManager(
            task_id=run_id,
            base_dir=parent_session_dir / "subagents",
        )
        self.session_mgr.init_session(model=model)

        # Per-session overflow callback. Honors CONTEXT_WINDOW_OVERRIDE for
        # testing parity with the main agent (see openclaw_agent.py:208-215).
        ctx_override = os.environ.get("CONTEXT_WINDOW_OVERRIDE")
        system_prompt = _build_subagent_system_prompt(task)
        self.overflow_cb = ContextOverflowCallback(
            model=model,
            context_window=int(ctx_override) if ctx_override else None,
            instructions_tokens=len(system_prompt) // 4,
        )
        self.compaction_count = 0

        # Filtered tool list + litellm schema built once; reused every turn.
        self._filtered_tools = _filter_tools(tools)
        self._tool_schemas = _tools_to_litellm_schema(self._filtered_tools)
        self._tool_map = {t.name: t for t in self._filtered_tools}

        # Remaining fields (model, summary_model, max_steps, max_compactions,
        # thinking_params, summary_runtime, task) stored as private attributes.

    async def run(self) -> str:
        """Execute the session loop until:
        - the model emits a final text response with no tool calls, OR
        - max_steps is reached, OR
        - max_compactions is reached and overflow repeats.

        Returns the final result text. Raises on unrecoverable errors.
        Registry lifecycle (mark_running → complete/fail) is the caller's
        responsibility (keeps the function pure)."""
```

**Session scoping**: `SessionManager(task_id=run_id, base_dir=parent_session_dir / "subagents")` produces transcript at `tasks/<parent_task>/subagents/<run_id>/transcript.jsonl`. Keeps subagent data neatly co-located with the parent task.

**Initial message bootstrap** (at the top of `run()` — same shape as `subagent_general.py:140-143`):

```python
self._messages: list[dict[str, Any]] = [
    {"role": "system", "content": _build_subagent_system_prompt(self._task)},
    {"role": "user", "content": self._task},
]
```

**Loop skeleton** (copy-adapted from `subagent_general.py:run_general_subagent` + `agent_loop.py:run`):

```python
for step in range(self._max_steps):
    # 1. Proactive compaction check — only fires if a prior turn's
    #    on_llm_start set needs_compaction=True.
    if self.overflow_cb.needs_compaction and self.compaction_count < self._max_compactions:
        await self._compact_in_place()

    # 2. Pre-call token estimate — reuse the same callback the main agent uses.
    #    on_llm_start() updates overflow_cb.current_tokens, sets
    #    overflow_cb.needs_compaction for the NEXT iteration, and returns
    #    truncated messages (tool-result truncation runs inside).
    self._messages = await self.overflow_cb.on_llm_start(self._messages)

    # 3. API call with reactive-overflow retry.
    try:
        response = await litellm.acompletion(
            model=resolved.model,
            messages=self._messages,
            tools=self._tool_schemas or None,
            max_tokens=4096,
            temperature=1.0,
            **(self._thinking_params or {}),
        )
    except Exception as e:
        if is_context_overflow_error(str(e)) and self.compaction_count < self._max_compactions:
            self.overflow_cb.force_compaction()
            await self._compact_in_place()
            continue
        raise

    # 4. Usage accumulation (from response.usage, not overflow_cb).
    resp_usage = getattr(response, "usage", None)
    if resp_usage is not None:
        self.usage.input_tokens += int(getattr(resp_usage, "prompt_tokens", 0) or 0)
        self.usage.output_tokens += int(getattr(resp_usage, "completion_tokens", 0) or 0)

    choice = response.choices[0]
    assistant_content = choice.message.content or ""
    tool_calls = choice.message.tool_calls

    # 5. Transcript + in-memory append for the assistant turn.
    self._append_assistant(choice)   # writes session_mgr + self._messages

    # 6. Terminal: no tool calls => final text response.
    if not tool_calls:
        return assistant_content.strip()

    # 7. Execute each tool call inline — same pattern as
    #    subagent_general.py:191-210 — and append tool results to both the
    #    session transcript and the in-memory message list.
    for tc in tool_calls:
        result = self._execute_tool_call(tc)
        self._append_tool_result(tc, result)

# Loop exhausted.
return "(subagent reached max steps without a final response)"
```

Three token-tracking notes for the implementer:

- `overflow_cb.on_llm_start(messages)` is the single source of truth for `current_tokens` and `needs_compaction`. Do NOT manually write to `_current_tokens` — keep the callback as the authority. This mirrors how `OpenClawComputerAgent` uses the callback (`agent_loop.py:169` → `self._on_llm_start`).
- `SubagentUsage` tracks *response*-reported tokens (billing-oriented). `overflow_cb.current_tokens` tracks *estimated* tokens including an inflation factor (`SAFETY_MARGIN`) and image overhead. Two separate counters, by design.
- The initial `instructions_tokens=len(system_prompt) // 4` passed to `ContextOverflowCallback.__init__` covers the system prompt; the callback re-estimates full message state on every `on_llm_start`.

**Compaction** (`_compact_in_place`):
- Extract messages from `self.session_mgr.load_history()` (same helper as main agent: `_extract_messages_for_compaction`).
- Call `compact_messages(messages, summary_model, overflow_cb.context_window, instructions_tokens=...)`.
- Append `CompactionSummaryBlock` entry to transcript via `session_mgr.append_compaction(...)`.
- Rebuild in-memory `messages` list from `[summary_block, ...kept_messages]`.
- Reset overflow callback, increment counter.

**`run_general_subagent()` becomes a thin wrapper** preserving the public shape used by US-SUB-005:

```python
async def run_general_subagent(*, task, model, tools, registry, run_id,
                                memory_store, summary_model, parent_session_dir,
                                max_steps=50, thinking_params=None):
    try:
        registry.mark_running(run_id)
        session = GeneralSubagentSession(
            run_id=run_id, task=task, model=model, tools=tools,
            registry=registry, memory_store=memory_store,
            summary_model=summary_model, parent_session_dir=parent_session_dir,
            max_steps=max_steps, thinking_params=thinking_params,
        )
        result = await session.run()
        registry.complete(run_id, result, session.usage)
    except Exception as e:
        registry.fail(run_id, str(e), getattr(session, "usage", SubagentUsage()))
        raise
```

Public signature gains `memory_store`, `summary_model`, `parent_session_dir` parameters. US-SUB-005's `DelegateGeneralTool` provides them from its own constructor.

**Acceptance criteria (proposed)**:
1. **Level 1 — Lint**: `uv run ruff check submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/subagent_session.py`
2. **Level 1 — Unit**: `GeneralSubagentSession` runs a multi-turn loop with mocked tool calls and returns the final text.
3. **Level 1 — Unit**: When `overflow_cb.needs_compaction` is forced true, `_compact_in_place()` rewrites the message list with a summary + preserved recent turns, increments `compaction_count`, and the loop continues.
4. **Level 1 — Unit**: Reactive overflow path — if `litellm.acompletion` raises a context-overflow-shaped error, compaction triggers and the call retries. After `max_compactions` retries, the error propagates.
5. **Level 1 — Unit**: Subagent transcript is written to `<parent_session_dir>/subagents/<run_id>/transcript.jsonl` with proper entries for assistant/tool/compaction messages.
6. **Level 1 — Unit**: `run_general_subagent` wrapper preserves registry lifecycle (mark_running → complete on success; fail on exception).
7. **Level 1 — Regression**: existing `tests/test_subagent_general.py` is updated to match the new signature; tool-filtering and system-prompt tests still pass.
8. **Level 1 — Default max_steps bumped** from 5 to 50; the loop no longer emits the "reached max steps" sentinel under normal use.
9. **Level 2**: None required at this layer — US-SUB-005's Level 2 exercises the combined path.
10. **Level 1 — Unit**: Pre-call `overflow_cb.on_llm_start(messages)` runs every iteration; a mock that sets `needs_compaction=True` at turn N triggers `_compact_in_place()` at turn N+1.
11. **Level 1 — Unit**: `GeneralSubagentSession.usage` is a public `SubagentUsage`; token counts from `response.usage.prompt_tokens/completion_tokens` are added after each LLM call; `session.usage` reflects the running total when `run()` returns.
12. **Level 1 — Unit**: `__init__` honors `CONTEXT_WINDOW_OVERRIDE` env var (matching `openclaw_agent.py:208-215`) when constructing the per-session `ContextOverflowCallback`.

### Files touched
- **NEW** `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/subagent_session.py`
- **MODIFY** `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/subagent_general.py` — thin wrapper calling `GeneralSubagentSession`; keep `_filter_tools`, `_build_subagent_system_prompt`, `ALLOWED_TOOL_NAMES`, `EXCLUDED_TOOL_NAMES`.
- **MODIFY** `tests/test_subagent_general.py` — update for new wrapper signature.
- **NEW** `tests/test_subagent_session.py` — direct coverage of the engine.

---

## US-SUB-005 (Revised) — Delegation Tools & Main Loop Integration

### Change vs. original
- **`depends`**: add **US-SUB-008** alongside US-SUB-002, US-SUB-004.
- **`DelegateGeneralTool`**: spawns an `asyncio.Task` wrapping the new `run_general_subagent(...)` wrapper, which internally runs `GeneralSubagentSession`. Interface from the main agent's perspective is unchanged (same params, same accepted/rejected return).
- **Default `max_steps`** in `DelegateGeneralTool` bumped 5 → 50.
- **New constructor arg** on `DelegateGeneralTool`: `parent_session_dir: Path` (forwarded to the session).
- **Drain semantics unchanged**: a completed session still pushes a single result to `registry.completion_queue` once; the subagent's own transcript lives on disk for later inspection.
- **AGENTS.md wording**: "session persists until the task is answered or cancelled" replaces any language implying the subagent is a single API call.

Everything else in the plan (SubagentsTool, DelegateGUITool, registry `attach_task`/`kill_run`, `_drain_completions` in `OpenClawComputerAgent`, AGENTS.md delegation section) stays as previously drafted.

### Files to Modify

| File | Change |
|------|--------|
| `openclaw/subagent_tools.py` | **NEW** — three `BaseTool` subclasses; `DelegateGeneralTool` takes `parent_session_dir` and `memory_store` in constructor |
| `openclaw/subagent_registry.py` | Add `attach_task(run_id, task)` + `kill_run(run_id)` |
| `openclaw/tools.py` | `build_tools()` accepts `registry` + `parent_session_dir`; appends three delegation tools |
| `openclaw/agent_loop.py` | `OpenClawComputerAgent.__init__` accepts `registry`; adds `_drain_completions(new_items)`; call site between `_handle_item` loop and proactive compaction (current line 238) |
| `openclaw_agent.py` | Instantiate `SubagentRegistry`; pass to `build_tools()` and `OpenClawComputerAgent`; pass `parent_session_dir=session_mgr.session_dir` |
| `openclaw/AGENTS.md` | Add "Delegation" section |
| `openclaw/__init__.py` | Export `SubagentRegistry`, new tool classes |
| `tests/test_subagent_tools.py` | **NEW** — unit tests for all three tools |
| `tests/test_subagent_drain.py` | **NEW** — unit tests for `_drain_completions()` |

### Delegation Tools — Revised Detail

**`DelegateGeneralTool`**

Constructor: `(registry, tools, memory_store, default_model, summary_model, parent_session_dir, thinking_params=None)`.

`call()`:
1. Parse `{task: str, model?: str, max_steps?: int=50, label?: str}`.
2. `run = registry.register(type=GENERAL, task=task, label=label, model=model or default_model)`. On `SubagentLimitError` → `{"status": "rejected", "reason": "max concurrent subagents reached"}`.
3. Build coroutine `run_general_subagent(task=task, model=..., tools=self._tools, registry=self._registry, run_id=run.run_id, memory_store=self._memory_store, summary_model=self._summary_model, parent_session_dir=self._parent_session_dir, max_steps=max_steps, thinking_params=self._thinking_params)`.
4. `loop = asyncio.get_running_loop(); task = loop.create_task(coro); registry.attach_task(run.run_id, task)`.
5. Return `{"status": "accepted", "run_id": run.run_id, "note": "persistent session — result auto-announces when complete; do not poll"}`.

**`DelegateGUITool`** — unchanged from original plan. GUI subagent remains a single blocking relay (no session engine upgrade — its multi-turn loop is short-lived and already has image pruning).

**`SubagentsTool`** — unchanged. `kill` action now has real teeth: `registry.kill_run(run_id)` cancels the running `asyncio.Task`, which raises `CancelledError` inside `GeneralSubagentSession.run()`. Session cleanup:

```python
# Inside the wrapper
except asyncio.CancelledError:
    registry.kill(run_id)  # transition to KILLED
    raise  # do NOT swallow — cooperative cancellation
```

### Acceptance Criteria (Revised)

All original Level 1 criteria from the PRD apply, plus:

- **Level 1**: `DelegateGeneralTool` constructor accepts `parent_session_dir` and forwards it to `run_general_subagent`.
- **Level 1**: Default `max_steps` for `DelegateGeneralTool` is 50 (not 5).
- **Level 1**: `SubagentsTool.call({action: "kill", target: run_id})` invokes `registry.kill_run(run_id)`, which cancels the `asyncio.Task` (verify via `task.cancelled()`).
- **Level 1**: `_drain_completions()` reads from `registry.completion_queue` and appends a user message per completed run with format `[Subagent Result] task: {task}\nStatus: {status}\n\n{result_text}`.
- **Level 2 — VM test** (`run_magic_tower.sh 50`): `trycua/.../turn_000/0000_api_start.json` contains `delegate_general`, `delegate_gui`, `subagents` in the `tools` array.
- **Level 2 — VM test**: If the agent invokes `delegate_general`, a subagent transcript appears at `tasks/<task>/subagents/<run_id>/transcript.jsonl` with at least one assistant entry.

---

## US-SUB-009 — Mid-Stream Steering (NEW — future, not in this cycle)

**Goal**: Let the main agent send follow-up messages into a running general subagent via `subagents(action=steer, target=<run_id>, message=<text>)`, matching OpenClaw's `steerControlledSubagentRun`.

**Design sketch** (not implemented now):
- `GeneralSubagentSession` gains `self._inbox: asyncio.Queue[str]`.
- Loop polls the inbox between turns (non-blocking `get_nowait()`); if present, append as user message before the next API call.
- `SubagentsTool` gains `steer` action that looks up the session handle (stored via `registry.attach_task` + a parallel `registry.attach_inbox(run_id, queue)`) and calls `queue.put_nowait(message)`.
- Concurrency guard: one steer per session turn — main agent can't spam.

**Dependencies**: US-SUB-005, US-SUB-008.

**Why deferred**: the V1 delegation pattern (spawn → async result → drain) is valuable on its own. Steering is a second-order capability that matters mainly for long interactive subagent sessions; let's validate V1 in real VM runs before building the steering channel.

---

## US-SUB-007 — Scope Note (revised intent)

Original US-SUB-007 covers **registry metadata** persistence (JSONL of run records). With US-SUB-008 introducing per-subagent transcripts on disk, US-SUB-007 should be extended — during the story itself — to:

1. Persist the registry's `SubagentRun` records (as originally scoped).
2. On resume, walk `tasks/<task>/subagents/*/` — any subagent dir whose registry record is `PENDING` or `RUNNING` at load time is stalled; mark `ERROR` with `error_message="stalled: prior session ended before completion"`.
3. Expose `completed_runs()` that includes a reference to the transcript path, so replayed context can link back to the subagent's detailed work rather than just its summary.

No changes needed now; capture this as an update to the US-SUB-007 PRD entry when we get there.

---

## Verification (US-SUB-008 → US-SUB-005 end-to-end)

After both stories land:

1. **Lint**: `uv run ruff check submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/subagent_session.py submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/subagent_tools.py tests/test_subagent_session.py tests/test_subagent_tools.py tests/test_subagent_drain.py`
2. **Unit**: `uv run pytest tests/test_subagent_session.py tests/test_subagent_general.py tests/test_subagent_tools.py tests/test_subagent_drain.py tests/test_subagent_registry.py tests/test_subagent_gui.py tests/test_subagent_gui_protocol.py -x`
3. **Level 2 VM**: `VM_IP=<ip> bash run_magic_tower.sh 50`. Confirm:
   - `trycua/.../turn_000/0000_api_start.json` — `delegate_general`, `delegate_gui`, `subagents` present in `tools`.
   - If the agent invokes `delegate_general`, a `tasks/<task>/subagents/<run_id>/transcript.jsonl` exists and contains at least an assistant final message.

## Risks / Open Questions

- **Subagent summary model**: US-SUB-008 needs a `summary_model` for its internal compactions. Default to the parent's `summary_model`. Acceptable because most delegated work is text-heavy and doesn't need a different summarizer.
- **Compaction → compaction amplification**: Subagent compactions summarize their own work; when they return their final text to the main agent, the main agent may later compact that too. Worst case: double summarization. Mitigation: none needed in V1 — content should still be useful. Flag to watch during `/judge`.
- **`SubagentsTool.kill` vs in-flight compaction**: if a kill arrives mid-`compact_messages()`, `asyncio.CancelledError` propagates cleanly; the subagent's transcript may show a partial compaction entry. Acceptable; document in the session's error path.
- **Session directory cleanup**: subagent dirs accumulate under `tasks/<task>/subagents/`. US-SUB-007 can layer a cleanup policy; not this story.

## Execution Order Summary

1. **US-SUB-008** — persistent session engine (prerequisite).
2. **US-SUB-005** — delegation tools + drain, wired to the engine from (1).
3. **US-SUB-006** — screenshot/state passing, as originally scoped.
4. **US-SUB-007** — registry + transcript persistence (scope note above).
5. **US-SUB-009** — steering (future).
