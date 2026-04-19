# US-SUB-003: GUI Subagent — Vision-to-Action Relay Protocol

## Context

The GUI subagent relay loop (US-SUB-004) needs a typed protocol layer to convert vision model responses into executable VM actions. This story creates the contract: GUIAction types, parsing logic for multiple response formats, and an execution function that maps actions to RemoteDesktopSession methods. No LLM calls, no VM interaction — just types, parsing, and conversion.

## OpenClaw Design Rationale

### What OpenClaw Does
OpenClaw is a personal AI assistant, not a GUI agent — there's no direct GUI-subagent counterpart in the OpenClaw source. What we borrow is the *subagent pattern* (focused worker, one-shot, constrained tool scope) from `openclaw/src/agents/subagent-announce.ts` and the `computer_call` format conventions from OpenAI's Responses API that GPT-5.4 emits natively.

### What We Keep and Why
- **Subagent pattern** — focused worker with a minimal system prompt, ephemeral lifetime, bounded steps.
- **Action type alignment with CUA's existing types** — CUA already defines `ClickAction`, `TypeAction`, etc. in `cua_bench/types.py`. Our GUIAction variants mirror those field names for consistency across the codebase.
- **OpenAI `computer_call` as primary parse path** — GPT-5.4 (the primary GUI subagent model) natively emits this format; supporting it first removes an unnecessary translation layer.
- **Direct session method calls** — execute via `RemoteDesktopSession.click()` etc., not via the Computer tool (which adds screenshot-after-action logic that the relay loop in US-SUB-004 handles itself).

### What We Drop and Why
- **No `move` or `screenshot` action types** — `move` is a no-op for task completion, and screenshots are taken by the relay loop after every action, not requested by the model.
- **No separate `double_click` GUIAction variant** — GPT-5.4 emits `double_click` in the native format; we fold it into `GUIClickAction` via `button="double"` to keep the union small and match the PRD's ClickAction(x, y, button) signature.

### Key Differences from OpenClaw
- CUA uses `RemoteDesktopSession` (RPC to a remote VM) rather than a direct `AsyncComputerHandler`.
- Session method signatures differ: `session.scroll(direction, amount)` vs handler `scroll(x, y, scroll_x, scroll_y)`.
- A function-calling fallback path for non-CU vision models is added — not present in OpenClaw.

## Implementation Plan

### File: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/subagent_gui_protocol.py`

#### 1. GUIAction types (dataclasses)

```python
@dataclass
class GUIClickAction:
    x: int
    y: int
    button: str = "left"  # "left" | "right" | "double"

@dataclass
class GUITypeAction:
    text: str

@dataclass
class GUIHotkeyAction:
    keys: list[str]

@dataclass
class GUIScrollAction:
    x: int
    y: int
    direction: str  # "up" | "down"
    amount: int = 3

@dataclass
class GUIDragAction:
    start_x: int
    start_y: int
    end_x: int
    end_y: int

@dataclass
class GUIWaitAction:
    ms: int = 1000

@dataclass
class GUIDoneAction:
    summary: str

GUIAction = Union[GUIClickAction, GUITypeAction, GUIHotkeyAction,
                  GUIScrollAction, GUIDragAction, GUIWaitAction, GUIDoneAction]
```

Field-name alignment with CUA `types.py`:
- `ClickAction(x, y)` → `GUIClickAction(x, y, button)` (adds button)
- `TypeAction(text)` → `GUITypeAction(text)` (same)
- `HotkeyAction(keys)` → `GUIHotkeyAction(keys)` (same)
- `ScrollAction(direction, amount)` → `GUIScrollAction(x, y, direction, amount)` (adds position for CU format)
- `DragAction(from_x, from_y, to_x, to_y)` → `GUIDragAction(start_x, start_y, end_x, end_y)` (PRD naming)
- `WaitAction(seconds)` → `GUIWaitAction(ms)` (ms per PRD)
- `DoneAction()` → `GUIDoneAction(summary)` (adds summary)

#### 2. Validation

`validate_gui_action(action: GUIAction) -> None` — raises `ValueError` on:
- negative coordinates (x, y, start_x, end_x, etc.)
- empty text (`GUITypeAction`)
- empty keys (`GUIHotkeyAction`)
- non-positive scroll amount
- non-positive wait ms

#### 3. `parse_gui_response(response) -> GUIAction`

Three parse paths, tried in order:

**(a) OpenAI computer_call output items** (native GPT-5.4 — primary):
- Input: response dict with `output` list containing `computer_call` items.
- Extract `action.type`:
  - `click` → `GUIClickAction(x, y, button)` (button defaults to "left")
  - `double_click` → `GUIClickAction(x, y, button="double")`
  - `type` → `GUITypeAction(text)`
  - `keypress` → `GUIHotkeyAction(keys)`
  - `scroll` → `GUIScrollAction(x, y, direction, amount)` — derive direction from sign of scroll_y (positive → down, negative → up) and amount from abs magnitude
  - `drag` → `GUIDragAction(start_x, start_y, end_x, end_y)` — take first/last point of `path`
  - `wait` → `GUIWaitAction(ms=1000)`

**(b) function_call with `gui_action` tool** (non-CU models):
- Input: response with `tool_calls` containing a `gui_action` function.
- Parse JSON arguments: `{action_type: "click", x: 100, y: 200, ...}`.
- Map to the corresponding GUIAction.

**(c) Text fallback** (structured keywords — last resort):
- Input: response text content.
- Regex patterns: `CLICK x y [button]`, `TYPE "text"`, `HOTKEY a+b+c`, `SCROLL direction amount`, `DRAG x1 y1 -> x2 y2`, `WAIT ms`, `DONE summary`.

Raises `ValueError` if no path produces a parseable action.

#### 4. `execute_gui_action(action, session) -> str | None`

Maps GUIAction → `RemoteDesktopSession` method calls:

| GUIAction | Session call | Notes |
|-----------|--------------|-------|
| `GUIClickAction(button="left")` | `session.click(x, y)` | |
| `GUIClickAction(button="right")` | `session.right_click(x, y)` | |
| `GUIClickAction(button="double")` | `session.double_click(x, y)` | |
| `GUITypeAction` | `session.type(text)` | |
| `GUIHotkeyAction` | `session.hotkey(keys)` | |
| `GUIScrollAction` | `session.scroll(direction, amount)` | Ignores x,y (session.scroll doesn't take position) |
| `GUIDragAction` | `session.drag(start_x, start_y, end_x, end_y)` | |
| `GUIWaitAction` | `await asyncio.sleep(ms / 1000)` | Not a session method |
| `GUIDoneAction` | **No VM call** | Returns `summary` |

Returns `None` for executed actions, the `summary` string for DoneAction.

#### 5. `gui_action_tool_schema() -> dict`

Returns the function-calling tool schema for non-CU vision models (see parse path (b)). Single `gui_action` function with an `action_type` discriminator plus optional per-variant fields.

### File: `tests/test_subagent_gui_protocol.py`

Covers all six Level-1 acceptance criteria:

1. **GUIAction union** — all 7 variants construct correctly.
2. **computer_call parsing** — every action type, including `double_click` → `button="double"` and scroll sign-to-direction conversion.
3. **function_call parsing** — `gui_action` tool call JSON → each variant.
4. **text fallback parsing** — each keyword → correct variant.
5. **execute_gui_action dispatch** — each variant maps to the correct (mocked) session method.
6. **DoneAction terminal** — returns summary; no session method called.
7. **Validation** — negative coords, empty text/keys, zero/negative scroll amount → `ValueError`.

## Key Files

| File | Action |
|------|--------|
| `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/subagent_gui_protocol.py` | **Create** |
| `tests/test_subagent_gui_protocol.py` | **Create** |
| `submodules/cua/libs/cua-bench/cua_bench/types.py` | Read-only reference |
| `submodules/cua/libs/python/agent/agent/responses.py` | Read-only reference |
| `submodules/cua/libs/cua-bench/cua_bench/computers/remote.py` | Read-only reference |

## Verification

1. `uv run ruff check submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/subagent_gui_protocol.py tests/test_subagent_gui_protocol.py`
2. `uv run pytest tests/test_subagent_gui_protocol.py -v`
3. All six acceptance-criteria categories pass.
