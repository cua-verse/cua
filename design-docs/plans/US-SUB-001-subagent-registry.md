# US-SUB-001: Subagent Base Types & Registry — Implementation Plan

## Context

The main agent loop currently has no mechanism to delegate work to subagents. US-SUB-001 lays the foundation — a `SubagentRun` dataclass and `SubagentRegistry` class with lifecycle management and a completion queue. This is the base layer that US-SUB-002 through US-SUB-006 build on.

## OpenClaw Design Rationale

### What OpenClaw Does
OpenClaw has a complex subagent registry (`subagent-registry.ts`, ~25 files) with:
- `SubagentRunRecord` type with 30+ fields (session keys, announce retries, frozen results, attachments, wake-on-descendant-settle)
- Disk-backed persistence via `subagent-registry-state.ts`
- Gateway-mediated spawn/announce via RPC
- Lifecycle events with retry/grace periods
- Deep nesting (depth-based roles, recursive delegation)

### What We Keep and Why
- **SubagentRun dataclass** with core fields: run_id, type, task, label, model, status, result_text, created_at, ended_at, usage — these are the minimum for lifecycle tracking and result delivery
- **Status enum** (pending → running → complete/error/killed) — matches OpenClaw's lifecycle state machine
- **Completion queue** (asyncio.Queue) — maps OpenClaw's announce queue to our async model; general subagent results are pushed here and drained between main agent steps
- **Concurrency limit** (max_concurrent, default 3) — prevents runaway API costs, same concept as OpenClaw's `maxChildrenPerAgent`
- **Registry API** (register, complete, fail, kill, list_runs, get_run, drain_completions) — same verbs as OpenClaw

### What We Drop and Why
- **Disk persistence** — subagent runs are ephemeral within a single `perform_task()` call; no need for cross-run restore (PRD notes confirm this)
- **Gateway/RPC routing** — we use asyncio.Task, not multi-session gateway calls
- **Session keys and requester chains** — no multi-session architecture; simple run_id tracking
- **Announce retry/backoff/grace** — our completion queue is in-process; no network delivery failures
- **Depth-based roles** — V1 is depth-1 only (no recursive delegation)
- **Wake-on-descendant-settle** — no nested subagents to wait for
- **Frozen result text / fallback captures** — simple: result_text set once on complete
- **Attachments, cleanup, archiving** — not applicable to ephemeral in-memory runs

### Key Differences from OpenClaw
- OpenClaw's registry persists runs to disk (survives restarts) and uses gateway lifecycle event listeners to detect completion. Ours is purely in-memory (ephemeral within one `perform_task()` call) with direct method calls — no event bus needed since subagents are `asyncio.Task` objects in the same process.
- OpenClaw uses `steer()` for mid-stream injection. We drain completions between steps (simpler, no streaming injection needed).
- OpenClaw supports arbitrary nesting depth. We're depth-1 only for V1.

## Implementation

### New file: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/subagent_registry.py`

```python
# Key types and classes:

class SubagentType(str, Enum):
    GENERAL = "general"
    GUI = "gui"

class SubagentStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    ERROR = "error"
    KILLED = "killed"

@dataclass
class SubagentUsage:
    input_tokens: int = 0
    output_tokens: int = 0

@dataclass
class SubagentRun:
    run_id: str
    type: SubagentType
    task: str
    label: str
    model: str
    status: SubagentStatus = SubagentStatus.PENDING
    result_text: str | None = None
    error_message: str | None = None
    created_at: str = ""    # ISO timestamp
    ended_at: str | None = None
    usage: SubagentUsage = field(default_factory=SubagentUsage)

class SubagentRegistry:
    def __init__(self, max_concurrent: int = 3):
        self._runs: dict[str, SubagentRun] = {}
        self._completion_queue: asyncio.Queue[SubagentRun] = asyncio.Queue()
        self._max_concurrent = max_concurrent

    def register(self, type, task, label, model) -> SubagentRun
    def mark_running(self, run_id) -> None
    def complete(self, run_id, result_text, usage?) -> None
    def fail(self, run_id, error_message, usage?) -> None
    def kill(self, run_id) -> None
    def active_count(self) -> int   # pending + running
    def get_run(self, run_id) -> SubagentRun | None
    def list_runs(self, status_filter?) -> list[SubagentRun]
    def drain_completions(self) -> list[SubagentRun]
    def to_snapshot(self) -> list[dict]  # for transcript observability
```

Design notes:
- `register()` checks `active_count() >= max_concurrent` and raises `SubagentLimitError` if exceeded. Concurrency limit applies only to GENERAL type (GUI is blocking, always 0-1 active).
- `complete()` and `fail()` push to `_completion_queue` only for GENERAL type (GUI returns directly via tool call).
- `kill()` sets status to KILLED and `ended_at`. The actual asyncio.Task cancellation is handled by the caller (US-SUB-002/005).
- `drain_completions()` uses `get_nowait()` loop — non-blocking, returns all available.
- `to_snapshot()` serializes all runs for session transcript observability.
- Run IDs generated via `uuid.uuid4().hex[:12]` prefixed with `"sub-"`.

### New file: `tests/test_subagent_registry.py`

Test cases matching each acceptance criterion:
1. SubagentRun dataclass — fields, defaults, serialization
2. register() — creates run with PENDING status, rejects when at limit
3. mark_running() — transitions PENDING → RUNNING
4. complete() — sets result_text, status=COMPLETE, pushes to queue (general only)
5. fail() — sets error_message, status=ERROR, pushes to queue (general only)
6. kill() — sets status=KILLED, sets ended_at
7. active_count() — counts PENDING + RUNNING
8. list_runs() — with and without status_filter
9. get_run() — found and not found
10. drain_completions() — returns all queued, empty after drain
11. Concurrency — general subagents respect limit, GUI subagents don't count
12. to_snapshot() — serialization round-trip

## Verification

1. **Lint**: `uv run ruff check submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/subagent_registry.py tests/test_subagent_registry.py`
2. **Tests**: `uv run pytest tests/test_subagent_registry.py -v`
3. No VM test needed — this is pure data types + in-memory registry (Level 1 only per acceptance criteria)
