# US-SUB-010 — GUI Subagent Unified Computer Tool Schema

## Context

The GUI subagent currently uses a custom `gui_action` tool schema (`subagent_gui_protocol.py`) that diverges from the main agent's `computer` tool schema (`unified.py:_build_computer_tool_schema`). This forces the vision model to learn a different interface (different action names, field names, scroll model) and causes bugs (keypress vs hotkey confusion, ARROWLEFT not normalized, scroll format mismatch). Merging to the same schema lets the model's existing `computer` tool training transfer directly.

## OpenClaw Design Rationale

**What OpenClaw does**: OpenClaw doesn't have a separate GUI subagent tool schema — it uses the same computer-use tool across all contexts. The CUA main agent's `_build_computer_tool_schema` (unified.py:29-129) defines the canonical schema; `agent.py:798-810` defines the `action_param_map` dispatch pattern.

**What we keep**:
- The `computer` tool schema (action enum, field names, parameter layout) from `unified.py`
- The `action_param_map` + `getattr`-style dispatch pattern from `agent.py:798-828`
- The `done` action (GUI subagent relay termination — not in OpenClaw, specific to our relay design)

**What we drop**:
- The 7 GUIAction dataclasses (GUIClickAction, GUITypeAction, etc.) — replaced by plain dicts
- The `gui_action` tool schema and its `action_type` field — replaced by `computer` tool with `action` field
- The isinstance-based dispatch in `execute_gui_action` — replaced by `getattr(handler, action_type)(**params)` against `cuaComputerHandler`
- The separate `_normalize_key` in the protocol — `cuaComputerHandler.keypress()` normalizes internally
- The scroll direction/amount translation — `cuaComputerHandler.scroll(x, y, scroll_x, scroll_y)` matches the schema directly

**Key differences from main agent**:
- `done` action added to the enum (relay termination signal, no VM call)
- `cuaComputerHandler` created from `session.computer` at relay loop startup (not from tool schemas)
- Schema function is sync with optional `width`/`height`/`environment` params (defaults: 1024x768, windows). Relay loop queries real dimensions from the handler and passes them in.

## Dispatch Target: `cuaComputerHandler`

The GUI subagent receives a `RemoteDesktopSession` which exposes its underlying `Computer` SDK instance via `session.computer`. We create a `cuaComputerHandler` from it at relay loop startup:

```python
from agent.computers import make_computer_handler
handler = await make_computer_handler(session.computer)
```

This gives the GUI subagent the **exact same dispatch interface** as the main agent — `getattr(handler, action_type)(**params)`. No translation layer needed. Key methods:
- `click(x, y, button="left")`, `double_click(x, y)`, `right_click(x, y)`
- `keypress(keys)` — normalizes keys internally via `_normalize_key`
- `scroll(x, y, scroll_x, scroll_y)` — same as main agent
- `drag(start_x=, start_y=, end_x=, end_y=)` — supports both path and coordinate formats
- `move(x, y)`, `wait(ms=1000)`, `screenshot()`, `type(text)`

## Implementation

### Step 1: Refactor `subagent_gui_protocol.py`

**1a. Replace `gui_action_tool_schema()` with `computer_tool_schema()`**

Adopt the exact field layout from `unified.py:_build_computer_tool_schema` with these changes:
- Action enum: `click, double_click, right_click, type, keypress, scroll, move, drag, screenshot, wait, done` (full main agent set minus `terminate`, plus `done` for relay termination)
- `done` replaces `terminate` — same relay termination role, with `summary` field
- `screenshot` included for schema parity — relay loop handles it as a no-op action (screenshot is taken every turn anyway; model requesting one explicitly just gets the next screenshot without advancing the action)
- `move` included — model may need to hover (tooltips, dropdowns)
- Sync function with optional `width`, `height`, `environment` params. Description includes "Screen resolution: {width}x{height} pixels. Environment: {environment}." matching unified.py. Defaults: 1024, 768, "windows".
- Field name: `action` (not `action_type`)
- Accept both `keypress` and `hotkey` in the parser (backward compat), but the schema only advertises `keypress`
- Keep scroll fields as `scroll_x`/`scroll_y` (matching main agent schema)

**1b. Remove GUIAction dataclasses**

Delete all 7 dataclass definitions and the `GUIAction` Union type. Replace with plain `dict[str, Any]` throughout. Each action dict has `{"action": "<type>", ...params}`.

**1c. Simplify `parse_gui_response()` → returns `list[dict]`**

Three parse paths remain but all return `list[dict[str, Any]]` (action dicts):

- **Path (a) computer_call**: Extract action dicts from `computer_call` items. The item's inner `action.type` field maps to our `action` key. Already dict-based — mostly a rename.
- **Path (b) function_call**: Look for tool name `computer` (not `gui_action`). Extract `action` field from parsed JSON args. Also accept `gui_action` tool name + `action_type` field for backward compat during transition.
- **Path (c) text**: Keep as fallback. Return action dicts instead of GUIAction instances.

Normalization rules applied during parsing:
- `hotkey` → `keypress` (action name alias)
- Key names normalized via `_normalize_key` (ARROWLEFT→left, etc.)
- For computer_call scroll: `scroll_x`/`scroll_y` kept as-is (translation happens at execution)

**1d. Replace `validate_gui_action()` with `validate_action()`**

Dict-based validation checking the same rules:
- `action` field must be a known action type
- Coordinates non-negative where required
- `text` non-empty for type action
- `keys` non-empty for keypress action
- etc.

**1e. Replace `execute_gui_action()` with dict-based dispatch**

```python
# Action → relevant params (mirrors agent.py:798-810)
_ACTION_PARAM_MAP = {
    "click":        ["x", "y"],
    "double_click": ["x", "y"],
    "right_click":  ["x", "y"],
    "type":         ["text"],
    "keypress":     ["keys"],
    "scroll":       ["x", "y", "scroll_x", "scroll_y"],
    "move":         ["x", "y"],
    "drag":         ["start_x", "start_y", "end_x", "end_y"],
    "screenshot":   [],
    "wait":         ["ms"],
    "done":         ["summary"],
}
```

Dispatch logic — uses `cuaComputerHandler` (identical to main agent `agent.py:824-828`):

```python
action_type = action_dict["action"]
params = {k: v for k, v in action_dict.items() if k != "action" and k in _ACTION_PARAM_MAP.get(action_type, [])}

if action_type == "done":
    return action_dict.get("summary", "")  # terminal, no VM call
if action_type == "screenshot":
    return None  # relay loop takes screenshot after every batch anyway

handler_method = getattr(handler, action_type, None)
if handler_method is None:
    raise ValueError(f"Unknown action: {action_type}")
await handler_method(**params)
return None
```

No per-action translation needed — `cuaComputerHandler` handles key normalization, scroll format, drag coordinates, etc. internally.

**1f. Update `__all__` exports**

Remove all GUIAction types. Export: `computer_tool_schema`, `parse_gui_response`, `execute_gui_action`, `validate_action`.

### Step 2: Update `subagent_gui.py` (relay loop)

- Change imports: drop all GUIAction types, import new protocol API
- **Create `cuaComputerHandler`** at relay startup: `handler = await make_computer_handler(session.computer)`
- **Query real dimensions**: `w, h = await handler.get_dimensions(); env = await handler.get_environment()`
- Replace `gui_action_tool_schema()` → `computer_tool_schema(width=w, height=h, environment=env)`
- Replace `isinstance(a, GUIDoneAction)` → `a.get("action") == "done"`
- Replace `isinstance(a, GUI*Action)` checks in `_describe_action` → `a.get("action") == "..."` checks
- `execute_gui_action(action, handler)` — now takes dict + handler (not session)
- Update system prompt: replace "gui_action" references with "computer" tool, update action vocabulary (click, double_click, right_click, type, keypress, scroll, move, drag, screenshot, wait, done)
- Keep `session` parameter for `screenshot()` calls (relay loop takes screenshots via session, not handler — handler.screenshot() returns base64 string, session.screenshot() returns bytes)

### Step 3: Update `__init__.py` exports

Remove GUIAction type exports from `openclaw/__init__.py`. Add `computer_tool_schema` if needed.

### Step 4: Migrate tests (`test_subagent_gui_protocol.py`)

All 58 tests migrated to the new schema:
- Remove `GUIAction` type imports; use dicts for assertions
- `_computer_call()` helper: action dict format stays the same (already uses `type` field internally)
- `_function_call_output()`/`_function_call_tool_calls()`: change tool name `gui_action` → `computer`, field `action_type` → `action`
- `test_tool_schema_*`: verify `computer` tool name, `action` field with updated enum
- `test_execute_*`: pass action dicts instead of GUIAction instances
- `test_validate_*`: call `validate_action()` with dicts instead of GUIAction instances
- `test_parse_text_*`: unchanged (text parsing produces dicts now)

### Step 5: Update `test_subagent_gui.py`

- Mocked `parse_gui_response` return values change from GUIAction instances to dicts
- `isinstance(a, GUIDoneAction)` checks in assertions change to dict checks

### Step 6: Create smoke test (`smoke/vm_action_test.py`)

Level 2 acceptance criteria: "VM smoke test passes all action types". Create a script that:
1. Connects to VM via `VM_IP` env var
2. Tests each action type against a real session: click, keypress (single + combo), scroll, move, drag, type, wait, screenshot, done
3. Verifies no exceptions, prints results

## Files Modified

| File | Change |
|------|--------|
| `submodules/cua/.../openclaw/subagent_gui_protocol.py` | Major rewrite: remove dataclasses, adopt computer schema, dict-based dispatch |
| `submodules/cua/.../openclaw/subagent_gui.py` | Update imports, dict-based action checks, new system prompt |
| `submodules/cua/.../openclaw/__init__.py` | Update exports |
| `tests/test_subagent_gui_protocol.py` | Migrate all 58 tests to new schema |
| `tests/test_subagent_gui.py` | Update mocked return values |
| `smoke/vm_action_test.py` | New — Level 2 VM smoke test |

## Verification

1. **Level 1 — Lint**: `uv run ruff check submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/subagent_gui_protocol.py tests/test_subagent_gui_protocol.py`
2. **Level 1 — Tests**: `uv run pytest tests/test_subagent_gui_protocol.py tests/test_subagent_gui.py -v` — all tests pass
3. **Level 1 — Full suite**: `uv run pytest tests/ -v` — no regressions
4. **Level 2 — VM smoke**: `VM_IP=<ip> uv run python smoke/vm_action_test.py` — all action types pass
