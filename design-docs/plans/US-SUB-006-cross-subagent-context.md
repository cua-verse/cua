# US-SUB-006 — Cross-Subagent Context: Screenshot & State Passing

**Story**: As the agent harness, I want the main agent to pass screenshots to general subagents for analysis and receive a fresh screenshot after GUI subagent delegation completes, so that subagents have visual context and the main agent sees the updated VM state after delegation.

**Depends on**: US-SUB-005 (delegation tools + main loop integration).

---

## Goal

Close the visual-context gap between the main agent and its subagents:

1. **General subagent receives screenshots** — main agent can delegate "analyze this frame" work to the LLM-only general subagent by passing on-disk screenshot paths.
2. **GUI subagent prunes image history** — already implemented in `subagent_gui.py:_prune_images_to_last_n`; lock with a targeted test.
3. **Main agent receives fresh VM screenshot after GUI delegation** — closes the stale-state gap when control returns from a blocking `delegate_gui` call.

---

## OpenClaw Design Rationale

### What OpenClaw Does

OpenClaw's subagent runner (`openclaw/src/agents/pi-embedded-runner/`) spawns every subagent as a full session that inherits the parent's compaction pipeline and transcript machinery. For GUI-adjacent work it uses the same Computer/screenshot substrate as the parent — there is no hard "GUI vs. LLM-only" fork. Screenshots ride along in the session's message list naturally, and pruning is applied uniformly by the compaction pipeline.

### What We Keep and Why

- **Image pruning semantics matching `only_n_most_recent_images=3`** — keeps short-lived visual context without blowing the budget on repeated frames. Already in `subagent_gui.py:_prune_images_to_last_n`.
- **Post-delegation re-grounding** — OpenClaw's main loop re-grounds after any sub-run that mutated world state by re-reading state. For CUA that means a fresh screenshot — the cheapest and most faithful way to re-ground after a GUI subagent has moved the VM.

### What We Drop and Why

- **Shared Computer tools between parent and subagents** — CUA's general subagent is intentionally LLM-only (no VM access) for safety and context hygiene. Screenshot-as-data is the substitute: the parent *hands* the frame in; the subagent cannot take one itself.
- **Automatic screenshot inheritance per step** — we do not pass continuous frames into the general subagent (would blow the budget and defeat its "planning/analysis" role); one-shot attachment at spawn time only.

### Key Differences from OpenClaw

- `DelegateGeneralTool` parameter contract explicitly carries `screenshot_paths` — visible in the tool schema so the model understands it can delegate vision work. OpenClaw does not need this because subagents inherit screenshots via the gateway.
- Post-GUI-delegation screenshot is pushed through a new registry queue drained in `agent_loop.py` (same seam as `_drain_completions`), because CUA's main-agent items list is the only place a synthesized user message can land mid-run. OpenClaw's gateway-mediated history model does not need this hop.

---

## Injection Seam — Why a Registry Queue

The main agent's next `predict_step` sends a list like:

```json
[
  {"role": "system", "content": "..."},
  {"role": "assistant", "tool_calls": [{"id": "call_123", "function": {"name": "delegate_gui", ...}}]},
  {"role": "tool", "tool_call_id": "call_123", "content": "<tool result text>"}
]
```

A tool-result `content` is a **string** — it cannot carry an `image_url` block. Returning a base64 or path from the tool result lands it in that string, where the vision model sees only characters (the exact pathology the 2026-04-19 CUA fix eliminated for `action="screenshot"`).

The only shape vision models actually see is a separate user message with an `image_url` content block:

```json
{"role": "user", "content": [
  {"type": "text", "text": "[VM state after GUI delegation]"},
  {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
]}
```

Tools cannot append standalone user messages to the loop's `new_items` list — only `OpenClawComputerAgent.run()` can. So `DelegateGUITool` pushes a pre-built user-message dict onto a registry queue, and `agent_loop.py` drains it at the existing seam (`agent_loop.py:245`, right after `_handle_item` and before the proactive-compaction check). This mirrors the `_drain_completions` pattern landed in US-SUB-005 and preserves the drain-before-compact invariant (new messages count toward this iteration's token budget).

---

## Changes

### §1. `DelegateGeneralTool` accepts `screenshot_paths`

**File**: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/subagent_tools.py`

- Add `screenshot_paths` (array of absolute file paths, optional) to `DelegateGeneralTool.parameters`.
- Forward `screenshot_paths` through `run_general_subagent(...)` to `GeneralSubagentSession(...)`.
- Docstring note: the main agent already has paths on hand via the `[Screenshot saved to: <path>]` user-message hint injected by `_handle_item` (agent_loop.py:340-347).

### §2. `GeneralSubagentSession` accepts `initial_screenshot_paths`

**File**: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/subagent_session.py`

- `__init__(..., initial_screenshot_paths: list[str] | None = None)`.
- When paths are provided, read each file, base64-encode the PNG, and build the initial user message as list-content:
  ```python
  [
    {"type": "text", "text": task},
    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
    ...
  ]
  ```
- Unreadable/missing paths → emit a text block `[screenshot unavailable: <basename>]` for that slot rather than failing the whole spawn; log a warning.
- When `initial_screenshot_paths` is `None` or empty, preserve current string-content behavior (string task as the user `content`).
- Transcript `append_message("user", ...)` records the text task only (screenshots are ephemeral visual context; the on-disk originals are already preserved elsewhere).
- Add a small internal helper `_encode_image_url_from_path(path: str) -> dict | None` that returns the content block or `None` on failure. Reuse for §4.

**File**: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/subagent_general.py`

- Forward `initial_screenshot_paths` kwarg from the wrapper to the session.

### §3. GUI pruning test (lock existing behavior)

**File**: `tests/test_subagent_gui.py`

- Add a targeted test that builds a message list with more than 3 `image_url` blocks across multiple user messages, calls `_prune_images_to_last_n(messages, n=3)`, and asserts:
  - Exactly 3 `image_url` blocks remain, all at the tail.
  - Older blocks are replaced with `{"type": "text", "text": "[older screenshot omitted]"}` (`_IMAGE_PLACEHOLDER`).
  - Ordering of non-image blocks is preserved.
- This locks in the behavior used in `run_gui_subagent`'s per-turn call at `subagent_gui.py:389`.

### §4. Post-delegation screenshot injection

**File**: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/subagent_registry.py`

- Add `_post_delegation_queue: queue.Queue[dict]` in `SubagentRegistry.__init__`.
- `enqueue_post_delegation(self, message: dict) -> None` — append a pre-built user-message dict.
- `drain_post_delegation(self) -> list[dict]` — drain all pending messages FIFO via `get_nowait()` (mirrors the existing `drain_completions` contract).

**File**: `subagent_tools.py`

- In `DelegateGUITool.call`, after `summary = future.result()` succeeds:
  1. Take a fresh screenshot via the same sync→async pattern already in use (`ThreadPoolExecutor + asyncio.run`) — reuse the existing `loop`/`executor` paths.
  2. Persist the bytes to disk under a predictable path (e.g. `<parent_session_dir>/subagents/<run_id>/post_delegation.png`) so trajectory inspection can find it later.
  3. Build `{"role": "user", "content": [{"type": "text", "text": "[VM state after GUI delegation]"}, image_url_block]}` via the shared `_encode_image_url_from_path` helper.
  4. Call `registry.enqueue_post_delegation(msg)`.
- On error path (the `except Exception` branch where `run_gui_subagent` raised): **skip** injection — the main agent has the error text from the tool result and the VM state is not reliable.
- If the fresh screenshot itself fails (e.g. session error), log a warning and skip injection; return the normal `{"status": "complete", ...}` tool result anyway. The main agent can re-screenshot next step.

**File**: `agent_loop.py`

- Add sibling method `_drain_post_delegation(self, new_items: List[Dict[str, Any]]) -> None` that calls `self._registry.drain_post_delegation()` and extends `new_items` with each dict (no reformatting — the tool pushes pre-built shapes).
- Call it one line after `_drain_completions(new_items)` at `agent_loop.py:245`, preserving the drain-before-compact window. Order: completions first (general-subagent results), then post-delegation screenshots — so the GUI screenshot is the freshest user turn before `predict_step`.
- No-op guard: `if self._registry is None: return`.

---

## Acceptance Criteria Mapping

| PRD Criterion | Where satisfied |
|---|---|
| L1 lint — `uv run ruff check .` | All edits pass |
| L1: `DelegateGeneralTool` accepts `screenshot_paths`; injected as `image_url` in initial user message | §1 + §2; new tests in `tests/test_subagent_tools.py` + `tests/test_subagent_session.py` |
| L1: GUI relay prunes screenshot history to last 3 images, placeholder text for older | §3 test in `tests/test_subagent_gui.py` |
| L1: After `DelegateGUITool` completes, fresh `session.screenshot()` injected as user message with `[VM state after GUI delegation]` | §4; new tests in `tests/test_subagent_tools.py` + `tests/test_subagent_drain.py` + `tests/test_subagent_registry.py` |
| L2: `run_magic_tower.sh 50` — after any GUI delegation, main agent's next `predict_step` input includes a fresh screenshot | Level 2 VM run at the end; verify trajectory API payload |

---

## Test Plan

**New / updated unit tests** (all under `tests/`):

- `test_subagent_tools.py`
  - `DelegateGeneralTool` forwards `screenshot_paths` verbatim to the spawned coroutine.
  - `DelegateGUITool` success path: after relay completes, registry's post-delegation queue contains exactly one user message with `[VM state after GUI delegation]` + an `image_url` block.
  - `DelegateGUITool` relay-failure path: queue is empty (no injection on failure).
  - `DelegateGUITool` screenshot-failure path: tool still returns `{"status": "complete", ...}`; queue is empty; warning logged.

- `test_subagent_session.py`
  - `GeneralSubagentSession` with `initial_screenshot_paths=[...]` builds the initial user message as list-content with `text` + N `image_url` blocks.
  - `initial_screenshot_paths=None` preserves current string-content behavior.
  - Unreadable path produces `[screenshot unavailable: <basename>]` text block; other paths still encode.

- `test_subagent_gui.py`
  - `_prune_images_to_last_n` across multi-message input keeps last 3 images, replaces older with placeholder, preserves non-image block ordering.

- `test_subagent_registry.py`
  - `enqueue_post_delegation` + `drain_post_delegation` roundtrip: FIFO order, empty drain is no-op, drain clears the queue.

- `test_subagent_drain.py`
  - `_drain_post_delegation` appends post-delegation messages to `new_items` after `_drain_completions` so the GUI screenshot lands last.
  - No-op when registry is `None`.

**Level 2 VM test**: `bash run_magic_tower.sh 50` (or a smaller magic-tower run that reliably exercises `delegate_gui`). Inspect the trajectory turn following the GUI delegation:

- In `turn_NNN/NNNN_api_start.json`, the `messages` (or `input`) array must contain a user message with text `[VM state after GUI delegation]` and an `image_url` content block whose base64 decodes to a valid PNG.
- Confirm token counts are not dominated by base64 strings in tool-result fields (regression guard for the 2026-04-19 bug).

---

## Non-Goals (deferred)

- Persisting post-delegation screenshots as image artifacts in the transcript JSONL (we save to disk under `<parent_session_dir>/subagents/<run_id>/post_delegation.png` but the transcript only records the text component; the on-disk file is sufficient for debugging).
- Streaming intermediate GUI subagent frames back to the main agent (the entire point of `delegate_gui` being blocking is that the main agent does not want intermediate frames).
- Passing screenshots to `DelegateGeneralTool` *mid-session* (only at spawn — matches the one-shot "analyze this" use case; mid-stream steering is US-SUB-009).
- Letting the general subagent take its own screenshots (intentional — see "What We Drop and Why").

---

## Files Changed

- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/subagent_tools.py` — `DelegateGeneralTool.parameters`, `DelegateGUITool.call` post-success enqueue path
- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/subagent_session.py` — `initial_screenshot_paths` + `_encode_image_url_from_path` helper
- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/subagent_general.py` — forward `initial_screenshot_paths`
- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/subagent_registry.py` — post-delegation queue + drain
- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/agent_loop.py` — `_drain_post_delegation`, call next to `_drain_completions` at line 245
- `tests/test_subagent_tools.py`, `tests/test_subagent_session.py`, `tests/test_subagent_gui.py`, `tests/test_subagent_registry.py`, `tests/test_subagent_drain.py` — new unit tests
