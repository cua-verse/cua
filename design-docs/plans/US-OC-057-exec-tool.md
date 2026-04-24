# US-OC-057 — Remote-VM Exec Tool (non-GUI shell)

**Status**: Draft plan — awaiting approval
**Depends on**: none
**Precedes**: US-OC-061 (process mgmt, once exec gets `--background`)

---

## 1. Problem & scope

The agent can currently only drive the VM through the `computer` tool (GUI) or
narrow file I/O (`read`/`write`/`edit` from US-OC-055). Non-GUI work — listing a
directory, reading a JSON file's structure, launching an app via CLI, checking
an env var — still has to round-trip through screenshots, which is slow and
noisy.

This story adds an **`exec`** BaseTool that runs a single shell command inside
the Windows VM via the existing `computer-server` RPC and returns
`stdout`/`stderr`/`exit_code`. Out of scope per PRD notes:

- Background mode, PTY mode, elevated/sudo, approvals/allowlists, sandbox bridge
- Per-command VM-side timeout (no RPC support today — US-OC-061 territory)
- Multi-platform dialect switching — Windows-first, POSIX falls out for free
  because `run_command` uses the OS default shell

---

## 2. OpenClaw design rationale

### What OpenClaw does (`bash-tools.exec.ts`, 1790 lines)

A single `exec` tool with foreground + background paths. Foreground path:
spawn a subprocess, stream stdout/stderr into an aggregated buffer capped at
`DEFAULT_MAX_OUTPUT = 200_000` chars (env-overridable via
`PI_BASH_MAX_OUTPUT_CHARS`), truncate with `truncateMiddle` when the cap is
hit, return `{status, exitCode, durationMs, aggregated, cwd, timedOut}`.
Background path yields after `yieldMs` and hands supervision to `processTool`.

Around this core, the file adds: script-preflight path validation
(shell-bleed prevention), allowlist approvals with per-command IDs, PTY
fallback, docker sandbox routing, exec-host selection (`auto|sandbox|gateway|
node`), elevated-permission flow. All of those are gated on flags and only
fire when the full OpenClaw runtime is present.

### What we keep and why

| OpenClaw pattern | We adopt | Why |
|------------------|----------|-----|
| Foreground `command` param | ✅ | Core use case |
| `cwd` param (workdir) | ✅ | PRD explicitly lists this param |
| `timeout` param (seconds) | ✅ (client-side bound) | PRD explicitly lists this param |
| `DEFAULT_MAX_OUTPUT = 200_000` chars | ✅ | Same semantics — bound on the model-visible text |
| `truncateMiddle(stdout, cap)` + explicit marker | ✅ | Keeps the final exit/error lines visible; better than head-only |
| Structured return `{status, exit_code, duration_ms, stdout, stderr, truncated}` | ✅ | Mirrors OpenClaw's `ExecProcessOutcome` shape |
| Layer 2 prose in `_build_exec()` with polling guardrails | ✅ | PRD acceptance #5 + US-OC-068 authoring rule |

### What we drop and why

| OpenClaw pattern | We skip | Why |
|------------------|---------|-----|
| Background mode (`yieldMs`, `background`) | ✗ | Deferred to US-OC-061; needs per-task process registry in computer-server |
| PTY mode (`pty: true`) | ✗ | No `computer-server` PTY RPC; would require server changes |
| Elevated (`elevated: true`) | ✗ | Sudo/UAC interaction belongs in a separate security story |
| Approvals / allowlists (`ask`, `security`) | ✗ | PRD notes explicitly rule these out |
| Exec host routing (`host`, `node`) | ✗ | CUA is single-VM by construction |
| Docker sandbox bridge | ✗ | No CUA analogue |
| Script-preflight validation | ✗ | Shell-bleed concern assumes a host-side spawn with argv injection; `run_command` RPC is already a single-string channel. Not a faithfulness gap for our threat model. |
| `env` dict injection | ✗ (MVP) | `run_command` RPC doesn't forward env today. If needed, user can use `set KEY=VAL && cmd` in the command string. Future story if demand arises. |

### Key differences from OpenClaw

1. **Timeout is client-side only.** OpenClaw kills the subprocess it spawned
   locally; we only hold an `asyncio.wait_for` on the RPC await. On expiry the
   tool returns `{status: "failed", timed_out: true}` but the VM-side
   subprocess may keep running (leak) until process-management lands in
   US-OC-061. The docstring + Layer 2 prose will call this out so the model
   doesn't rely on timeout to truly kill runaway work.

2. **`cwd` is emulated via command wrapping**, not an RPC parameter. On Windows
   we prepend `cd /d "<cwd>" && ` (cmd.exe); on POSIX, `cd "<cwd>" && `. The
   workspace-root guard in `_assert_within_workspace` (reused from
   `tools_fs.py`) bounds `cwd` when `workspace_root` is set.

3. **Shell dialect is fixed by `run_command`.** Windows VM handler uses
   `asyncio.create_subprocess_shell` → cmd.exe. If the agent wants PowerShell
   it passes `powershell -NoProfile -Command "..."` explicitly. The Layer 2
   prose advises this.

---

## 3. Interface

### Schema (agent-facing)

```json
{
  "name": "exec",
  "parameters": {
    "type": "object",
    "properties": {
      "command": { "type": "string", "description": "Shell command (cmd.exe on Windows VM, /bin/sh on POSIX)." },
      "cwd":     { "type": "string", "description": "Working directory on the VM. Defaults to the shell's cwd." },
      "timeout": { "type": "number", "description": "Client-side timeout in seconds (default 60, max 300). On expiry the VM-side process may keep running." }
    },
    "required": ["command"]
  }
}
```

### Return shape

```python
# success
{
  "success": True,
  "status": "completed",
  "exit_code": 0,
  "duration_ms": 142,
  "stdout": "...",
  "stderr": "",
  "truncated": False,
  "cwd": "C:\\Users\\User\\Desktop\\tasks\\mota_24_easy"
}

# non-zero exit
{
  "success": True,             # tool ran successfully; command itself failed
  "status": "failed",
  "exit_code": 1,
  "duration_ms": 87,
  "stdout": "...",
  "stderr": "dir: not found",
  "truncated": False,
  "cwd": "..."
}

# truncated
{
  "success": True, "status": "completed", "exit_code": 0, "duration_ms": 4310,
  "stdout": "<first 100K chars>\n\n[... output truncated: 1.3 MB omitted ...]\n\n<last 100K chars>",
  "stderr": "...",
  "truncated": True,
  "cwd": "..."
}

# timeout
{
  "success": False,
  "status": "failed",
  "error": "Error: exec timed out after 60s (VM-side process may still be running)",
  "timed_out": True,
  "duration_ms": 60000,
  "cwd": "..."
}

# validation failure (cwd outside workspace, missing command, etc.)
{ "success": False, "error": "Error: ..." }
```

---

## 4. Implementation

### New file: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/tools_shell.py`

Structure mirrors `tools_fs.py`:

```
Constants
  _DEFAULT_MAX_OUTPUT_CHARS = 200_000       # matches PI_BASH_MAX_OUTPUT_CHARS default
  _DEFAULT_TIMEOUT_SECONDS = 60
  _MAX_TIMEOUT_SECONDS = 300

Helpers
  _is_windows_path(path: str) -> bool       # reused from tools_fs (or imported)
  _assert_within_workspace(...)             # reused via import from tools_fs
  _truncate_middle(s: str, cap: int) -> tuple[str, bool]
  _wrap_with_cwd(command: str, cwd: str | None, is_windows: bool) -> str
  _run_async(coro)                          # imported from tools_fs

@register_tool("exec")
class ExecTool(BaseTool):
    def __init__(self, interface, workspace_root=None,
                 max_output_chars=None, default_timeout=None, cfg=None): ...
    description = "Run a single shell command inside the remote VM and return stdout/stderr. Non-GUI only (GUI apps block)."
    parameters = { ... }  # as above
    def call(self, params, **kwargs) -> dict:
        parse + validate cwd path guard
        return _run_async(self._execute(...))
    async def _execute(...):
        wrapped = _wrap_with_cwd(command, cwd, is_windows)
        t0 = time.monotonic()
        try:
            result = await asyncio.wait_for(
                self.interface.run_command(wrapped),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            return {..., "timed_out": True, "duration_ms": int((monotonic()-t0)*1000)}
        stdout, truncated_out = _truncate_middle(result.stdout, cap)
        stderr, truncated_err = _truncate_middle(result.stderr, cap)
        return { ... }
```

**_truncate_middle**: keep `cap // 2` chars from each end, insert a marker
`\n\n[... output truncated: N chars omitted ...]\n\n`. Leaves the start and
final exit lines visible — matches OpenClaw's `truncateMiddle` behavior.

### Wire into `tools.py`

```python
from .tools_shell import ExecTool     # new import
# in build_tools(...):
exec_tool = ExecTool(session.interface, workspace_root=workspace_root)
tools: list = [
    computer, milestone_tool, analyze_image_tool,
    read_tool, write_tool, edit_tool,
    exec_tool,                                        # inserted after edit
    memory_search, memory_get, memory_write,
]
```

No new `build_tools()` kwargs — `workspace_root` is already threaded from
`openclaw_agent.perform_task`.

### Export from `__init__.py`

Add `ExecTool` to the public re-exports.

### Prompt integration: `prompt.py::_build_exec()`

New method, called from `build()` on the same gating pattern as `_build_memory`
/ `_build_delegation` — append only when `"exec" in tool_summaries`:

```
## Shell Execution

- exec runs a single non-GUI shell command inside the VM (cmd.exe on Windows,
  /bin/sh on POSIX) and returns stdout/stderr/exit_code. GUI apps launched
  via exec will block the call until they exit — use the computer tool or
  delegate_gui for GUI work.
- Prefer one command per call. Do NOT build tight polling loops with exec —
  long-running or background work is not supported yet; use a single
  deterministic command instead.
- `cwd` is emulated via a `cd` prefix; paths must resolve inside the task
  workspace (set by TASK_CATEGORY / TASK_TAG).
- `timeout` bounds the client-side wait only; the VM-side process may keep
  running if the timeout fires. Keep timeouts tight (default 60s, max 300s).
- On Windows prefer direct executables (`dir`, `type`, `where`, `python3`).
  If you need PowerShell, write `powershell -NoProfile -Command "..."`
  explicitly.
- Output is truncated in the middle once it exceeds ~200K chars; head + tail
  are preserved so exit/error lines stay visible.
```

The main `build()` method gains:

```python
if self.config.tools.enabled and tool_summaries:
    exec_lines = self._build_exec(tool_summaries)
    if exec_lines:
        parts.extend(exec_lines)
```

placed **after** `_build_tools` but **before** `_build_memory` (so the order
roughly matches OpenClaw's system-prompt interleave). Will slot between
existing sections cleanly.

### No AGENTS.md changes

Per US-OC-068 authoring rule + US-OC-055's corrected precedent: AGENTS.md
stays tool-name-free. All exec prose lives in `_build_exec()`.

---

## 5. Testing plan

### Level 1 — unit (new file `tests/test_exec_tool.py`)

Target ≈ 20 tests across 5 classes:

1. **`TestRegistration`** — `exec` appears in `TOOL_REGISTRY`, `ExecTool.name == "exec"`, included by `build_tools()` when invoked.
2. **`TestParamValidation`** — missing `command`, non-string `command`, `cwd` outside workspace, timeout out of range. Each returns `{success: False, error: "Error: ..."}`.
3. **`TestHappyPath`** (RPC mocked via `AsyncMock` on `interface.run_command`):
   - zero exit → `status: completed`, fields populated
   - non-zero exit → `status: failed`, `stdout`/`stderr` forwarded
   - empty stdout → returns empty string, not `None`
   - `cwd` wrapping: Windows path → `cd /d "<cwd>" && <cmd>`; POSIX path → `cd "<cwd>" && <cmd>`
4. **`TestTruncation`**:
   - stdout just under cap → untouched, `truncated=False`
   - stdout 3× cap → middle truncation, marker present, `truncated=True`, length roughly cap
   - `_truncate_middle` unit tests covering edge cases (empty, cap==0, cap larger than input)
5. **`TestTimeout`** (use `asyncio.Event` never-set to simulate hang):
   - timeout fires → `status: failed`, `timed_out: True`, `duration_ms ≈ timeout*1000`
   - default timeout applied when param omitted
   - timeout clamped to `_MAX_TIMEOUT_SECONDS`

Also update `tests/test_openclaw_tools.py` tool-count assertion: 9 → 10.

Run target: `uv run pytest tests/test_exec_tool.py -q` ≥ 20 passing; full
suite ≥ 1087 passing (1067 after US-OC-055 + ~20 new).

### Level 2 — VM smoke (`smoke/exec_tool_vm.py`)

Mirror `smoke/fs_tools_vm.py` structure. Against a live Windows VM:

1. `exec {"command": "dir"}` → non-empty stdout, exit 0
2. `exec {"command": "echo Hello from VM"}` → stdout contains "Hello from VM"
3. `exec {"command": "powershell -NoProfile -Command \"Get-Date -Format yyyy-MM-dd\""}` → stdout is a date
4. `exec {"command": "dir", "cwd": "C:\\Users\\User\\Desktop\\tasks\\helloworld"}` → cwd reflected
5. `exec {"command": "dir", "cwd": "C:\\Windows"}` with workspace set → `success: False`, error mentions workspace
6. `exec {"command": "ping -n 30 127.0.0.1", "timeout": 2}` → `timed_out: True`

Log to `logs/us_oc_057/smoke_<ts>.log`.

### Level 2 — agent trajectory (PRD acceptance #6)

```
bash run_magic_tower.sh 50
```

Grep the trajectory for at least one `exec` function call in
`turn_NNN/*_agent_response.json` with a non-empty stdout in the paired
`*_function_call_output.json`. Script it into a one-line assertion the
`/judge` run can verify.

### What we explicitly don't test at Level 2

- Background / long-running commands (out of scope)
- PTY-required programs (out of scope)
- Multi-VM / exec-host routing (out of scope)

---

## 6. Files touched (summary)

**New**
- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/tools_shell.py`
- `tests/test_exec_tool.py`
- `smoke/exec_tool_vm.py`

**Modified**
- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/tools.py` — import + slot `ExecTool` in `build_tools()`
- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/__init__.py` — re-export
- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/prompt.py` — `_build_exec()` + call site in `build()`
- `tests/test_openclaw_tools.py` — tool-count assertion 9 → 10

**Not modified**
- AGENTS.md (tool-name-free rule)
- `computer-server/handlers/*.py` (`run_command` RPC already exists)
- `openclaw_agent.py` (no new kwargs needed)

---

## 7. Open questions / risks

1. **Does `run_command` on the Windows VM honor a long-running foreground
   command without the RPC layer timing out before our client-side
   `asyncio.wait_for` does?** Needs a smoke check in step 6 of Level 2 to
   confirm the 2s timeout case actually surfaces from our side, not the
   transport's. If the RPC transport has its own timeout, the behavior is
   still safe (we catch and re-shape the error) but the `timed_out: True`
   signal may not be set correctly — will adjust if smoke uncovers this.

2. **GUI-app blocking**: `exec "notepad.exe"` on Windows will return only when
   Notepad closes. Acceptance criteria don't require preventing this, and the
   Layer 2 prose explicitly warns against it. If agents misbehave during
   Level 2, consider a follow-up story to add a server-side non-blocking
   spawn variant — not in this one.

3. **Encoding**: Windows handler tries `["utf-8", "gbk", "gb2312", "cp936",
   "latin1"]` and falls back to `utf-8` with `errors="replace"`. Good enough
   for English output; could surface mojibake on localized VMs. Flag only,
   not a blocker.

4. **Concurrent exec calls**: `asyncio.wait_for` cancels the RPC coroutine on
   timeout, but nothing serializes exec calls. Not a correctness issue — the
   computer-server handler spawns an isolated subprocess per call. Parallel
   calls from delegated subagents work by construction.

---

## 8. Implementation order

1. Write `tools_shell.py` with `ExecTool` + helpers
2. Wire into `tools.py` + `__init__.py`
3. Write `tests/test_exec_tool.py`, update tool-count test; run L1
4. Add `_build_exec()` in `prompt.py` + call site; add prompt-builder tests in `tests/test_prompt_builder.py`
5. Write `smoke/exec_tool_vm.py`; run L2 smoke
6. Run `bash run_magic_tower.sh 50` for trajectory-level L2
7. Update `progress.txt` story entry
8. `/judge` + `/ship`
