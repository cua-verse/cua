# US-SUB-007 — Subagent Registry Persistence (Disk-Backed Runs)

## Context

The `SubagentRegistry` is currently in-memory only — runs are ephemeral within a single `perform_task()` call. US-SUB-007 adds append-only JSONL persistence so that:
1. Run records survive across sessions (cross-run continuity)
2. Stalled async subagents from a crashed prior session are detected and marked orphaned on restart
3. Historical subagent results are queryable for context replay

## OpenClaw Design Rationale

**What OpenClaw Does**: `subagent-registry.store.ts` uses a single versioned JSON file (`runs.json`) with full-map overwrite on every save. `subagent-registry-state.ts:restoreSubagentRunsFromDisk` loads and merges into the in-memory map.

**What We Keep**: Persist-on-state-transition, restore-on-init, orphan detection (pending/running → error on restore). Same semantics, different format.

**What We Drop**: Versioned JSON with full-rewrite (wasteful for append-heavy benchmark runs). Multi-process cross-read via disk (we're single-process asyncio). Legacy field migration (no V1→V2 for us).

**Key Difference**: Append-only JSONL matching our established `SessionManager._append_entry()` pattern. Last-entry-wins dedup on load (natural consequence of append-only writes).

## Implementation Plan

### File: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/subagent_registry.py`

#### 1. Add imports
```python
import json
from pathlib import Path
```

#### 2. Add `SubagentRun.from_dict()` classmethod
Inverse of the existing `to_dict()`. Needed by `restore()`.

#### 3. Extend constructor
```python
def __init__(self, max_concurrent: int = 3, persist_path: Path | None = None) -> None:
    ...existing...
    self._persist_path = persist_path
```
When `None`, all disk methods are no-ops. Preserves backward compatibility with 53 existing tests.

#### 4. Add `_persist_run(run_id)` internal method
Append a single run's current state as one JSONL line. No-op when path is None.
Pattern: `self._persist_path.parent.mkdir(parents=True, exist_ok=True)`, open append, write `json.dumps(run.to_dict()) + "\n"`.

#### 5. Wire auto-persist into state transitions
- `register()` → `self._persist_run(run.run_id)` after creation
- `mark_running()` → `self._persist_run(run_id)` after status change
- `complete()` → `self._persist_run(run_id)` after status change
- `fail()` → `self._persist_run(run_id)` after status change  
- `kill()` → `self._persist_run(run_id)` after status change

**Why persist on register/mark_running?** Without it, runs that crash mid-flight would be invisible in the JSONL — making orphan detection impossible.

#### 6. Add `restore()` instance method
- No-op if `persist_path is None` or file doesn't exist
- Read all lines, skip blank/malformed, parse via `SubagentRun.from_dict()`
- Dedup: last entry per run_id wins
- Orphan: if `status in (PENDING, RUNNING)` → set status=ERROR, error_message="stalled: prior session ended before completion"
- Merge: loaded runs go UNDER current-session runs

#### 7. Add `completed_runs()` method
- Filter for terminal statuses (COMPLETE, ERROR, KILLED)
- Compute transcript path as `persist_path.parent / "subagents" / run_id / "transcript.jsonl"`
- Return `run.to_dict()` + `"transcript_path": str(path) if path.exists() else None`

### File: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw_agent.py`

At registry instantiation:
```python
persist_path = session_mgr.task_dir / "subagent-runs.jsonl"
registry = SubagentRegistry(persist_path=persist_path)
registry.restore()
```

### Tests: `tests/test_subagent_registry_persistence.py` (NEW, ~24 tests)

Four classes: TestPersistRun, TestRestore, TestCompletedRuns, TestRoundTrip.
