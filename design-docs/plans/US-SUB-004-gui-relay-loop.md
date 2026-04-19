# US-SUB-004 — GUI Subagent Relay Loop Implementation

## Context

US-SUB-003 shipped the **contract layer** for the GUI subagent: typed `GUIAction` variants, a three-path `parse_gui_response()` (OpenAI `computer_call` → `function_call` → text), an `execute_gui_action()` dispatcher to `RemoteDesktopSession`, and a `gui_action_tool_schema()` for function-calling.

US-SUB-004 builds the **active loop** on top of that contract: a blocking `run_gui_subagent()` coroutine that the main agent calls synchronously from a delegation tool (US-SUB-005). The loop takes a screenshot, asks a specialized vision model for the next action, executes it on the VM, and repeats until the model emits `DoneAction` or a hard limit trips. While it runs, the main agent's loop is paused — the VM is a single-writer resource.

The problem this solves: the main reasoning model (Claude Sonnet) is expensive and its context window is precious. Routine GUI chores — "open Notepad via the Start menu", "navigate to C:\foo", "dismiss this dialog" — can be delegated to a cheaper vision-focused model that runs in an isolated scratch context and returns a one-line summary. This keeps the main agent's transcript lean and shifts mechanical work onto a model optimized for it.

## OpenClaw Design Rationale

### What OpenClaw Does

OpenClaw's `subagent-spawn.ts` + `subagent-announce.ts` spawn a full `pi-embedded-runner` child — a recursively nested agent that inherits tools (minus a few excluded), runs its own streaming completion, and pushes announce events back to the parent via the gateway. Depth up to 3 levels. No special "GUI" variant — every subagent is a general-purpose model with the same tool surface, differentiated only by its task prompt.

OpenClaw does **not** have a dedicated vision-relay subagent — it runs in a desktop-agent environment (not VM-driven), so GUI automation happens through Claude's native computer-use API on the main loop itself.

### What We Keep and Why

- **Ephemeral worker framing** — `buildSubagentSystemPrompt` in `subagent-announce.ts:47-104` establishes "you are a focused worker, complete and return, don't initiate, be ephemeral." We keep this framing verbatim-in-spirit, trimmed for GUI scope.
- **Registry lifecycle** — `register → mark_running → complete/fail` matches OpenClaw's run-record lifecycle. GUI runs are tracked for observability but *not* pushed to the completion queue (GUI is blocking, result returns directly).
- **Hard `max_steps` cap** — matches US-SUB-002's decision: OpenClaw uses a 48h timeout, but for a mechanical vision loop we want a tighter bound. Default 15.
- **Usage accumulation on `SubagentRun.usage`** — mirrors OpenClaw's per-run token tracking.

### What We Drop and Why

- **Full pi-embedded-runner machinery** — no gateway, no streaming, no ACP routing, no nested tool inheritance. GUI subagent has *no* tools other than the `gui_action` function-calling tool. It cannot spawn children, read memory, or write files. It is a narrow vision-to-action translator.
- **Announce dispatch / retry** — GUI is blocking; the caller awaits directly. No need for `subagent-announce-dispatch.ts` logic.
- **Depth tracking** — depth-1 only (GUI subagents cannot spawn anything).
- **Abort infrastructure** — no `abortEmbeddedPiRun` equivalent. Cancellation will come in US-SUB-005 via `asyncio.Task.cancel()` wrapping a general delegation; GUI is blocking and short, so it simply runs to completion or `max_steps`.

### Key Differences from OpenClaw

- **Vision-first tool contract** — our GUI subagent is not a general LLM with a tool palette. It emits either a `gui_action` function call or (via the text fallback in `parse_gui_response`) a structured text block. Nothing else.
- **Screenshot-as-feedback loop** — fundamentally different data flow from OpenClaw (which is text-in text-out). Each iteration produces a fresh VM screenshot that becomes the next turn's input.

## Single-Path Request Building — Correction to the PRD

PRD acceptance criterion #5 says:

> model-aware request building: for computer-use models (openai/gpt-5.4), use native computer_call tool schema; for other vision models, use function-calling with gui_action tool

This is wrong on two counts:

1. CUA's `agent/loops/openai.py:137-141` (`_is_native_tool_model`) explicitly excludes GPT-5.4 from native `computer_use_preview`:
   ```python
   # GPT 5.4 does NOT support computer_use_preview - it uses function calling
   return bool(re.search(r"computer-use-preview", model, re.IGNORECASE))
   ```
2. We have no `computer-use-preview` model available in our account, so the native branch cannot be VM-verified. Shipping an untested branch violates the "no broken code" rule.

**Decision**: Ship a **single primary path** — `litellm.acompletion` with the `gui_action` function-calling tool. All vision models (`gpt-5.4`, `gpt-4o`, `ui-tars-*`, Claude vision) route through this one path. `parse_gui_response` already handles the returned shape. A future story can add the native `computer_use_preview` branch once such a model is available for end-to-end testing.

Criterion #5 becomes moot; test 9 asserts the single path is the one in use.

## Implementation

### File to create: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/subagent_gui.py`

Public API:

```python
async def run_gui_subagent(
    *,
    instruction: str,
    session: Any,                       # RemoteDesktopSession
    registry: SubagentRegistry,
    run_id: str,
    model: str = "openai/gpt-5.4",
    max_steps: int = 15,
    thinking_params: dict | None = None,
) -> str:
    """Blocking vision-to-action relay loop. Returns the final summary."""
```

Internal structure:

1. **`_build_system_prompt()`** — minimal, GUI-focused. Adapted from `subagent-announce.ts:73-103`. Describes the role ("you are a GUI automation subagent"), the action vocabulary (click/type/hotkey/scroll/drag/wait/done), the termination rule (emit `done` with a summary when the instruction is complete), and the visibility model ("you see a screenshot each turn; you cannot reason across turns beyond what you emit").

2. **`_action_key(action: GUIAction) -> tuple`** — stable hashable key for dedup. Example: `("click", 50, 75, "left")`. `GUIWaitAction` is *exempt* from dedup (legitimate polling pattern).

3. **`_describe_action(action: GUIAction) -> str`** — one-line human-readable form for the action log text, e.g. `"click (50, 75) left"`, `"type 'Hello'"`, `"done: Settings app opened"`.

4. **`_prune_images_to_last_n(messages, n=3)`** — walks messages newest → oldest, keeps the first `n` image blocks encountered, strips earlier image blocks while preserving the surrounding text (replaces the image block with `{"type": "text", "text": "[older screenshot omitted]"}`). Matches `only_n_most_recent_images=3` semantics already used in `openclaw_agent.py:231`.

5. **`_encode_screenshot(png_bytes: bytes) -> dict`** — returns an `image_url` content block with `data:image/png;base64,...` URL.

6. **`_accumulate_usage(usage, response)`** — extract `usage.prompt_tokens` + `usage.completion_tokens` from the litellm response and add to the running `SubagentUsage`.

7. **Main loop** (single path — `litellm.acompletion`):
   ```
   registry.mark_running(run_id)
   messages = [system, user(instruction)]
   initial_screenshot = await session.screenshot()
   messages.append(user([text "Current screenshot:", image(initial_screenshot)]))
   action_keys: list[tuple] = []

   for step in range(max_steps):
       response = await litellm.acompletion(
           model=model, messages=messages,
           tools=[gui_action_tool_schema()],
           max_tokens=1024, temperature=0.3,
           **(thinking_params or {}),
       )
       _accumulate_usage(usage, response)

       try:
           action = parse_gui_response(response.choices[0])
       except ValueError as e:
           registry.fail(run_id, f"parse error: {e}", usage)
           raise

       if isinstance(action, GUIDoneAction):
           summary = action.summary or "(no summary)"
           registry.complete(run_id, summary, usage)
           return summary

       if not isinstance(action, GUIWaitAction):
           key = _action_key(action)
           action_keys.append(key)
           if len(action_keys) >= 3 and len(set(action_keys[-3:])) == 1:
               msg = f"stuck in loop: action {_describe_action(action)!r} repeated 3 times"
               registry.fail(run_id, msg, usage)
               raise StuckInLoopError(msg)

       messages.append(_assistant_tool_call_message(response.choices[0]))
       messages.append(_tool_result_message(response.choices[0], "ok"))

       await execute_gui_action(action, session)
       new_screenshot = await session.screenshot()
       messages.append({"role": "user", "content": [
           {"type": "text", "text": f"After action {_describe_action(action)}. Current screenshot:"},
           _encode_screenshot(new_screenshot),
       ]})
       _prune_images_to_last_n(messages, n=3)
   else:
       partial = f"max_steps ({max_steps}) reached without completion"
       registry.complete(run_id, partial, usage)
       return partial
   ```

8. **Error handling** — any exception from `session.*`, `litellm.*`, or `parse_gui_response` is caught; `registry.fail(run_id, str(e), usage)` is called, then the exception is re-raised so the delegation tool can format it as a tool error result.

### Test file to create: `tests/test_subagent_gui.py`

Ten unit tests (all with mocked `litellm` and mocked `session`):

1. **Loop lifecycle** — happy path: two actions (click, type) then `DoneAction("opened")`; assert `registry.get_run(run_id).status == COMPLETE`, `result_text == "opened"`, usage non-zero, `session.click` + `session.type` each called once, three `session.screenshot()` calls (initial + 2 post-action).

2. **Screenshot feedback grows the transcript** — intercept `messages` on each call; assert each iteration's messages contain the screenshot taken *after* the prior action (or the initial screenshot for iteration 0).

3. **Image pruning** — drive 5 iterations before `Done`; on the 5th call, assert exactly 3 `image_url` content blocks remain in `messages`; older ones replaced with `"[older screenshot omitted]"`.

4. **Done termination** — `Done` on first step returns its summary without executing any action; `session.click/type/...` never called.

5. **Max-steps termination** — mock returns non-Done for `max_steps=3` iterations; assert return string contains `"max_steps"`, `registry.get_run(run_id).status == COMPLETE`, result_text records partial completion.

6. **LLM error → registry.fail** — mock `litellm.acompletion` to raise `RuntimeError("network")`; assert `registry.get_run(run_id).status == ERROR`, `error_message` contains `"network"`, exception propagates out of `run_gui_subagent`.

7. **Action dedup / stuck-in-loop** — mock returns the same click action 3 times; assert `StuckInLoopError` raised, `registry.get_run(run_id).status == ERROR`, `error_message` contains `"stuck in loop"` and the action description. Separate assertion: three `GUIWaitAction`s do *not* trigger dedup.

8. **Usage accumulation** — mock two responses with known `prompt_tokens`/`completion_tokens`; after `Done`, assert `registry.get_run(run_id).usage.input_tokens == sum`, same for output.

9. **Request shape** — model=`"openai/gpt-5.4"`; assert `litellm.acompletion` (not `aresponses`) was called with `tools=[{..."function": {"name": "gui_action", ...}}]`.

10. **Parse failure → registry.fail** — mock response that parses to nothing (empty `output`, no `tool_calls`, no text); assert `ValueError` from `parse_gui_response` → `registry.fail`, status ERROR.

### Files to import/reuse

- `subagent_gui_protocol`: `GUIAction`, `GUIClickAction`, ..., `GUIDoneAction`, `parse_gui_response`, `execute_gui_action`, `gui_action_tool_schema`, `validate_gui_action`.
- `subagent_registry`: `SubagentRegistry`, `SubagentUsage`, `SubagentStatus`, `SubagentRun`.

### Files *not* modified in this story

- `agent_loop.py` — integration with main loop is US-SUB-005.
- `tools.py` / `build_tools()` — delegation tool registration is US-SUB-005.
- `subagent_gui_protocol.py` — the contract already exists; we only consume it.
- `AGENTS.md` — delegation guidance lives in US-SUB-005.

## Acceptance Criteria Mapping

| # | Criterion | Covered by |
|---|-----------|------------|
| 1 | Lint passes | `ruff check` on new files |
| 2 | Loop: screenshot → messages → call → parse → execute → repeat | tests 1, 2 |
| 3 | Screenshot fed back; pruned to last 3 | tests 2, 3 |
| 4 | Terminates on Done / max_steps / error | tests 4, 5, 6 |
| 5 | Model-aware request building | **Superseded** — single path; test 9 asserts the path; `/ship` notes the correction |
| 6 | Action dedup safety | test 7 |
| 7 | Token usage accumulated per-step in SubagentRun.usage | test 8 |
| 8 | Level 2 VM test — GUI subagent opens Notepad | `smoke/gui_subagent_notepad.py` |

## Level 2 VM Test

A minimal standalone script at `smoke/gui_subagent_notepad.py` (new, outside the pytest harness):

- Reads `VM_IP` env var; constructs `RemoteDesktopSession(api_url=..., os_type="windows")` and calls `await session.start()`.
- Constructs a `SubagentRegistry`, calls `registry.register(type=GUI, task="Open Notepad via the Start menu.", model="openai/gpt-5.4")` to get a `run_id`.
- Calls `run_gui_subagent(instruction=..., session=..., registry=..., run_id=..., model="openai/gpt-5.4", max_steps=15)`.
- On completion, saves the final screenshot to `smoke/out/notepad_final.png`. Prints the summary + step count + token usage.

Pass criterion: final screenshot shows a Notepad window open (manual inspection).

## Verification

1. `uv run ruff check submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/subagent_gui.py tests/test_subagent_gui.py smoke/gui_subagent_notepad.py`
2. `uv run pytest tests/test_subagent_gui.py -v` — all 10 unit tests pass.
3. `uv run pytest tests/test_subagent_gui_protocol.py tests/test_subagent_registry.py tests/test_subagent_general.py -v` — no regressions in sibling subagent tests.
4. Level 2 smoke: `VM_IP=… uv run python smoke/gui_subagent_notepad.py` — verify Notepad screenshot.
5. `/judge` for critical review (registry lifecycle, dedup correctness, pruning, prompt quality).

## Risks & Open Questions

- **GPT-5.4 availability in our environment** — if GPT-5.4 isn't reachable, the Level 2 smoke falls back to `openai/gpt-4o` (which has vision but weaker UI grounding). Will flag this during `/judge` if it comes up.
- **Coordinate calibration** — GPT-5.4's click coordinates assume a specific viewport/DPR. The VM screenshot dimensions must match what the model was trained on. If the first Notepad test misses the Start button, this is the first place to look.
- **Stuck-in-loop false positives** — three identical `WaitAction(1000)` calls are legitimate (polling). Mitigated: `GUIWaitAction` is exempt from dedup.
- **Native `computer_use_preview` branch** — deferred to a future story gated on availability of such a model in our account.
