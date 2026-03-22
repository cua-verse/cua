# US-OC-034: Expose Trajectory Screenshot Path in Computer Call Output

## Problem

Every computer call automatically takes a screenshot and returns it as inline base64. `TrajectorySaverCallback` saves this screenshot to a local file (e.g., `trycua/.../turn_005/0003_screenshot_after.png`), but the agent never learns this path.

After compaction, the base64 image is removed from context. With `analyze_image` (US-OC-033), the agent *could* re-examine old screenshots — but only if it knows the local file path.

## Solution

Override `_on_screenshot` and `_handle_item` in `OpenClawComputerAgent` to inject screenshot paths as user messages. No CUA core changes.

### What the agent sees after this story

After each computer call, the agent receives the base64 image (as before) **plus** a user message:

```
[Screenshot saved to: /Users/.../trycua/.../turn_005/0003_screenshot_after.png]
```

This text (~100 chars) survives both image retention and compaction summarization.

## Implementation (completed)

**Single file changed**: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/agent_loop.py`

Three methods added to `OpenClawComputerAgent`:

### 1. `_on_screenshot` override

After `super()._on_screenshot()` dispatches to all callbacks (including `TrajectorySaverCallback` which saves the file), reads the path from the callback's internal state.

```python
async def _on_screenshot(self, screenshot, name="screenshot"):
    await super()._on_screenshot(screenshot, name)
    self._last_screenshot_path = self._resolve_screenshot_path(name)
```

### 2. `_handle_item` override

After the parent builds `[computer_call_output]`, appends a user message with the path:

```python
async def _handle_item(self, item, computer=None, ignore_call_ids=None):
    self._last_screenshot_path = None
    result = await super()._handle_item(item, computer, ignore_call_ids)
    if self._last_screenshot_path and result:
        result.append({
            "role": "user",
            "content": f"[Screenshot saved to: {self._last_screenshot_path}]",
        })
        self._last_screenshot_path = None
    return result
```

### 3. `_resolve_screenshot_path` helper

Reads the path from `TrajectorySaverCallback`'s state. Returns `None` when no trajectory saver exists (graceful degradation).

```python
def _resolve_screenshot_path(self, name: str) -> str | None:
    from agent.callbacks.trajectory_saver import TrajectorySaverCallback
    for cb in self.callbacks:
        if isinstance(cb, TrajectorySaverCallback) and cb.trajectory_id:
            turn_dir = cb._get_turn_dir()
            idx = cb.current_artifact - 1  # just incremented by _save_artifact
            return str(turn_dir / f"{idx:04d}_{name}.png")
    return None
```

## Design decisions

### Why user message, not modifying `computer_call_output`

The Responses API has a fixed schema for `computer_call_output`. Adding extra fields (e.g., `screenshot_path`) would be ignored by the API — the model wouldn't see them. A separate user message is valid in the items list and visible to the model.

### Why user message survives image retention

`ImageRetentionCallback` removes old `computer_call` + `computer_call_output` pairs but leaves other items untouched. The user message with the path is a separate item, so it persists even after old images are pruned.

### Callback ordering

`TrajectorySaverCallback` is auto-added by `ComputerAgent.__init__()` when `trajectory_dir` is set. Our `_on_screenshot` override calls `super()` first, which dispatches to all callbacks in list order. Since `TrajectorySaverCallback` is added before our callbacks, it saves the file before we read the path.

### Dependency on TrajectorySaverCallback internals

We depend on `_get_turn_dir()` and `current_artifact` which are not public API. Mitigated by pinning the CUA submodule version.

## Risks

1. **Compaction summary quality**: The path text must survive summarization. If the summarizer drops it as noise, post-compaction recall fails.
2. **Path format**: Local absolute paths. Only valid on the same machine. This is always true in our setup.

## Relationship to US-OC-033

US-OC-033 (`analyze_image`) provides the tool. This story provides the paths. Together they enable:

```
Turn 5:  computer_call → screenshot saved → agent sees image + path text
Turn 20: compaction → image removed, path text survives in summary
Turn 25: agent calls analyze_image(image="/path/from/turn_5/screenshot.png", prompt="...")
```
