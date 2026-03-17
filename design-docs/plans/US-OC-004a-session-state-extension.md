# US-OC-004a: Session State Schema Extension

## Context

Compared our `state.json` schema against OpenClaw's `SessionEntry` (60+ fields). Identified gaps relevant to CUA: cache token tracking, model in state, system prompt reporting. Also identified `run_number` in `SessionState` as redundant — JSONL session headers already mark run boundaries and `openclaw_agent.py` never reads `run_number` from state.

## Files to modify

- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/session.py` — all 4 changes
- `tests/test_openclaw_session.py` — update existing + add new tests
- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/__init__.py` — export `build_system_prompt_report`

## Changes

### 1. Remove `run_number` from SessionState

- Remove field from `SessionState`, `to_dict()`, `from_dict()`
- `init_session()`: derive run_number for JSONL session header via new `_count_session_headers()` method (counts `"type": "session"` lines in transcript)
- `load_history(run_number=N)` unchanged — it reads run_number from JSONL headers, not state.json
- Tests: update assertions to check transcript headers instead of `state.run_number`

### 2. Add cache token fields to TokenUsage

Add `cache_read: int = 0`, `cache_write: int = 0` to TokenUsage. Update `accumulate()` with optional kwargs (backward-compatible defaults), `to_dict()`, `from_dict()`.

**Note:** `contextTokens` (context window size / model capacity) is a top-level SessionState field, NOT in TokenUsage. This matches OpenClaw's `contextTokens: 200000` on the session entry. TokenUsage tracks only cumulative API usage.

### 3. Add `model` and `contextTokens` to SessionState

Add `model: str = ""` and `contextTokens: int = 0`. Set in `init_session(model=...)`. `contextTokens` stores the model's context window size (e.g. 200000), matching OpenClaw's top-level `contextTokens` field on the session entry. Update serialization.

### 4. Add `system_prompt_report` to SessionState

- Add `system_prompt_report: dict[str, Any] | None = None` to SessionState
- Add `SessionManager.set_system_prompt_report(report)` method
- Add `build_system_prompt_report()` module-level helper in session.py

**Report schema:**
```python
{
    "source": "run",
    "generated_at": 1710000000.0,
    "system_prompt": {
        "chars": 5000,
        "project_context_chars": 2000,
        "non_project_context_chars": 3000
    },
    "injected_files": [
        {"name": "AGENTS.md", "raw_chars": 1500, "injected_chars": 1500, "truncated": False}
    ],
    "tools": {
        "entries": [
            {"name": "computer", "summary_chars": 100, "schema_chars": 500, "properties_count": 3}
        ]
    }
}
```

**`build_system_prompt_report()` signature:**
```python
def build_system_prompt_report(
    *,
    system_prompt: str,
    context_files: list[Any] | None = None,
    tool_summaries: dict[str, str] | None = None,
    tools: list[Any] | None = None,  # BaseTool instances for schema extraction
    source: str = "run",
) -> dict[str, Any]:
```

- Measures total prompt chars, finds `# Project Context` header to split project vs non-project
- Context files: raw_chars from content, injected_chars measured in prompt, truncated = injected < raw
- Tools: duck-typed — if tool has `.parameters`, extract schema_chars and properties_count; otherwise summary only
- No BaseTool import in session.py (uses `hasattr` duck-typing)

## Implementation order

1. Change 2 (cache tokens) — standalone
2. Change 3 (model) — standalone
3. Change 1 (run_number removal) — most test churn
4. Change 4 (system_prompt_report) — most complex

## Verification

```bash
uv run ruff check submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/session.py tests/test_openclaw_session.py
uv run pytest tests/test_openclaw_session.py -v
```

Also verify backward compat: old state.json without new fields loads without error (from_dict defaults).
