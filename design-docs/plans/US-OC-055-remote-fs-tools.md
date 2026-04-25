# US-OC-055 — Tool: Remote-VM File I/O (read + write + edit)

## Context

The main agent currently has no way to read or mutate files on the remote Windows VM without driving the GUI. Every inspection of a task artifact (save file, config, output dump) costs a screenshot turn plus VLM analysis; every edit requires opening Notepad via `delegate_gui`. The US-OC-031 audit flagged remote-VM `read`/`write`/`edit` as the **largest single capability gap** (Category 1, P1) and recommended a three-in-one port because all three share the same `session.interface` plumbing and can be exercised by a single Level 2 VM test.

CUA's `BaseComputerInterface` already exposes the required RPCs against the computer-server (`read_bytes`, `write_bytes`, `read_text`, `write_text`, `file_exists`, `create_dir`, `list_dir`), so this is pure BaseTool work — no computer-server changes. The acceptance criteria add one new constraint not found in existing tools (MilestoneTool, AnalyzeImageTool): **path policy must reject absolute paths outside the VM's task workspace**, so the tools need workspace_root wired in.

## OpenClaw Design Rationale

### What OpenClaw Does
OpenClaw wraps `@mariozechner/pi-coding-agent`'s `createReadTool` / `createWriteTool` / `createEditTool` via `pi-tools.read.ts::createHostWorkspace{Read,Write,Edit}Tool(root, {workspaceOnly})`. Read does adaptive byte-paging (`10 %` of context window), edit does exact-match string replacement with a recovery wrapper (`pi-tools.host-edit.ts::wrapEditToolWithRecovery`) that re-reads the file on mismatch and either retroactively declares success (if the edit already applied) or appends current-file snippet to the error message. Required-param groups are defined in `pi-tools.params.ts:75-85`.

### What We Keep and Why
- **Tool surface**: three distinct tools (`read`, `write`, `edit`) — matches the OpenClaw and Claude Code mental models and the tool-migration audit's recommendation. Agents are trained on this shape.
- **Edit API**: `edits: [{oldText, newText}]` array of exact-match replacements. Exact-match (no regex, no line numbers) is what `pi-coding-agent` ships and what agents handle most reliably.
- **Required-param groups** mirror `REQUIRED_PARAM_GROUPS`: read requires `path`; write requires `path` + `content`; edit requires `path` + non-empty `edits` with valid `{oldText, newText}` entries (validator matches `isValidEditReplacement`).
- **Edit mismatch-hint recovery** (from `pi-tools.host-edit.ts`): when an edit fails "text not found", re-read the file and return `oldText not found. Current file contents (first 800 chars): ...` — critical for agent self-correction. Also implement the "edit likely applied" retry guard (if `newText` is now present in the file and `oldText` is gone, the edit already succeeded).
- **`workspaceOnly` semantics**: boundary-enforced mode mirrors OpenClaw's `createHostWorkspaceReadTool(root, {workspaceOnly: true})`.
- **Adaptive byte-paging on `read`** (`executeReadWithAdaptivePaging`, `pi-tools.read.ts:206-282`): **keep faithfully.** When `limit` is not supplied, compute `max_bytes = clamp(context_window_tokens * 4 * 0.10, 32 KB, 128 KB)` (matches `resolveAdaptiveReadMaxBytes` at :69-82), page the underlying read up to 4 times, concatenate page text with `"\n\n"` delimiters, strip the `"Use offset=N to continue"` notice between pages, and append a final `[Read output capped at <bytes> for this call. Use offset=N to continue.]` message when capped. An explicit `limit` skips the loop (single page, user-chosen slice). Context window is threaded from `ContextOverflowCallback.context_window` via a new `context_window_tokens` kwarg on `build_tools()`.
- **Image sanitization on `read`** (`normalizeReadImageResult` + `sanitizeToolResultImages`, `pi-tools.read.ts:292-349, :693`): **keep faithfully.** When the path's extension is an image type (reuse `_MIME_MAP` from `analyze_image.py:37-47`), read via `interface.read_bytes`, then: (a) MIME-sniff from magic bytes (PNG 89 50 4E 47, JPEG FF D8 FF, GIF 47 49 46 38, WEBP "RIFF…WEBP", BMP "BM", TIFF II*/MM*) and **rewrite the declared MIME if the sniff disagrees**, matching `normalizeReadImageResult`; (b) enforce a per-image byte cap (`max_bytes`, default 10 MB same as `DEFAULT_MAX_BYTES_MB`); (c) return an image content block `{"type": "image", "data": base64, "mime_type": sniffed_mime}` alongside a text header (`Read image file [image/png]`) — exact parity with `pi-coding-agent`'s read output shape. This coexists with `AnalyzeImageTool`: `read` hands the image to the model for direct inspection; `analyze_image` asks a separate VLM and returns text-only — both are legitimate OpenClaw-faithful paths.
  - **CUA surfacing adapter** (small, in-scope): CUA function_call outputs are stringified into `function_call_output.output`, which would drown the image block. Mirror the existing `action=="screenshot"` sentinel pattern (`progress.txt:28-40`, `agent.py:866-880`) in reverse: when a tool returns a dict with `{"type": "image", "data": b64, "mime_type": mime}`, the `_handle_item` function_call branch emits a sentinel `{"success": True, "read_image": True, "mime_type": mime}` into `function_call_output.output` AND appends a separate `user` message with an `input_image` content block carrying the base64 (same shape as how click/keypress surface a paired screenshot today). This keeps the screenshot-whale fix invariant (no base64 in tool-result content) while delivering the image to the model. ~15 lines in `submodules/cua/libs/python/agent/agent/agent.py`, guarded on a typed-dict check so non-image tool results flow through unchanged.

### What We Drop and Why
- **Sandbox bridge path** (`createSandboxed*Tool`): OpenClaw's docker-sandbox variant has no analogue in CUA — we only run against a live Windows VM via `session.interface`.
- **`wrapToolParamValidation`** wrapper module: CUA's `BaseTool._verify_json_format_args` already normalizes `str|dict` params. We replicate the *required-group* validation inline (≈15 lines) rather than porting the entire wrapper.
- **Edit post-write retroactive-success inference** (`didEditLikelyApply`, `pi-tools.host-edit.ts:76-112`): the OpenClaw recovery catches the class where the underlying writeFile succeeded but the *tool* threw — possible on the Node fs layer (async write + stat race). CUA's `interface.write_text` over computer-server is a single-shot RPC that either completes or raises, so the race doesn't exist on our transport. Keep the mismatch-hint recovery (the common agent-correction case); drop the post-write readback-for-success inference. Re-add in a follow-up if a CUA failure class surfaces.
- **`apply_patch`-style patch diffs**: explicitly out of scope for US-OC-055; tracked as US-OC-059.
- **URL/file-URL handling and `@`-prefix stripping** in `read` (`resolveToolPathAgainstWorkspaceRoot`): OpenClaw supports `file://…` URLs and `@path` syntax. Dropped for now — agents in our benchmark use plain Windows/POSIX paths. Add if we see agents emitting either form in trajectories.

### Key Differences from OpenClaw
- **Remote VM, not host FS**: all paths target the VM via `session.interface.*` RPCs. No `fs.readFile` / `fs.writeFile`. Path detection uses `_is_windows_path` (already proven in `milestone.py:95` and `analyze_image.py:58`).
- **BaseTool sync `call()` wraps async RPCs**: use the `ThreadPoolExecutor().submit(asyncio.run, coro)` pattern documented at `progress.txt:4` and implemented in `analyze_image.py:149-170`. Per-tool duplication of ~20 lines is acceptable; refactoring into a shared helper is out of scope.
- **Encoding**: `read_text`/`write_text` on CUA default to UTF-8. Add optional `encoding` kwarg on read (default `utf-8`); write always writes UTF-8 (no encoding param — agents write text; binary writes are out of scope).
- **No edit-recovery read-back snapshot** for success inference when the underlying write succeeded: CUA's `write_text` either succeeds or raises, so the "write succeeded but reported error" class doesn't occur — we keep mismatch-hint recovery but drop the post-write retry.

## Implementation Plan

### New file: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/tools_fs.py`

One module for all three tools (they share path-policy + Windows path detection + sync→async helper + MIME sniffer). Structure:

```python
# Module-level helpers & constants
_MAX_IMAGE_BYTES_DEFAULT = 10 * 1024 * 1024      # matches analyze_image DEFAULT_MAX_BYTES_MB
_MAX_MISMATCH_HINT_CHARS = 800                    # matches OpenClaw EDIT_MISMATCH_HINT_LIMIT
_DEFAULT_READ_PAGE_MAX_BYTES = 32 * 1024          # matches OpenClaw DEFAULT_READ_PAGE_MAX_BYTES
_MAX_ADAPTIVE_READ_MAX_BYTES = 128 * 1024         # matches OpenClaw MAX_ADAPTIVE_READ_MAX_BYTES
_ADAPTIVE_READ_CONTEXT_SHARE = 0.10               # matches OpenClaw ADAPTIVE_READ_CONTEXT_SHARE
_CHARS_PER_TOKEN_ESTIMATE = 4                     # matches OpenClaw CHARS_PER_TOKEN_ESTIMATE
_MAX_ADAPTIVE_READ_PAGES = 4                      # matches OpenClaw MAX_ADAPTIVE_READ_PAGES
_MIME_MAP = {...}                                 # reuse from analyze_image.py:37-47 (consider promoting to shared util)

def _is_windows_path(path: str) -> bool: ...      # mirror milestone.py:95 / analyze_image.py:58
def _normalize_path(path: str) -> str: ...        # ntpath.normpath for Windows
def _assert_within_workspace(path, workspace_root): ...  # raises ValueError
def _run_async(coro): ...                         # executor pattern (analyze_image.py:149-170)
def _parent_dir(path, is_windows): ...            # ntpath.dirname | posixpath.dirname
def _mime_from_extension(path: str) -> str | None: ...
def _sniff_mime_from_bytes(data: bytes) -> str | None: ...  # magic-byte sniff (PNG/JPEG/GIF/WEBP/BMP/TIFF)
def _format_bytes(n: int) -> str: ...             # matches OpenClaw formatBytes:84-92
def _resolve_adaptive_read_max_bytes(ctx_tokens: int | None) -> int: ...  # mirrors resolveAdaptiveReadMaxBytes

# Tool classes
@register_tool("read")
class ReadFileTool(BaseTool):
    def __init__(self, interface, workspace_root=None, context_window_tokens=None, cfg=None): ...

@register_tool("write")
class WriteFileTool(BaseTool):
    def __init__(self, interface, workspace_root=None, cfg=None): ...

@register_tool("edit")
class EditFileTool(BaseTool):
    def __init__(self, interface, workspace_root=None, cfg=None): ...
```

**ReadFileTool schema**
- `path` (required, string) — Windows or POSIX absolute path on the VM
- `offset` (optional, int, default 1) — 1-based start line
- `limit` (optional, int, default ∅ → adaptive paging kicks in) — max lines to return
- `max_bytes` (optional, int) — for **image** reads, per-image byte cap (default 10 MB). For text reads, the cap is computed adaptively from `context_window_tokens` (see below) unless overridden.
- `encoding` (optional, string, default `"utf-8"`) — text decode encoding

**Image path** (file extension matches `_MIME_MAP`):
1. `data = await interface.read_bytes(path)` — single call, no paging.
2. Reject if `len(data) > max_bytes` (default 10 MB) with a formatted-size error (see `analyze_image.py:254-262`).
3. MIME-sniff from magic bytes; if sniff disagrees with extension-derived MIME, use the sniffed value and rewrite the text header. If sniff yields a non-`image/*` MIME, return `Error: file looks like <sniffed> but was treated as <expected> (<path>)` (matches `normalizeReadImageResult:319-323`).
4. Return `{"success": True, "type": "image", "data": base64, "mime_type": sniffed_mime, "text": f"Read image file [{sniffed_mime}]"}`. CUA's `_handle_item` adapter (see "CUA surfacing adapter" below) turns this into a sentinel function_call_output + a paired `user`/`input_image` message.

**Text path** (everything else):
1. `text = await interface.read_text(path, encoding=encoding)`. If UnicodeDecodeError, return `Error: file is not UTF-8 text (<path>) — use analyze_image for images or pass encoding='<other>'`.
2. Split on `\n`; `total_lines = len(lines)`.
3. If caller supplied explicit `limit`: return the slice `lines[offset-1 : offset-1+limit]`, flag `truncated` when `offset-1+limit < total_lines`. Skip adaptive paging.
4. **Adaptive byte-paging** (no explicit `limit`):
   - `cap = clamp(context_window_tokens * 4 * 0.10, 32_768, 131_072)` — matches `resolveAdaptiveReadMaxBytes`. Falls back to 32 KB when `context_window_tokens` is None.
   - Iterate up to 4 pages starting at `offset`. Each page grabs up to `DEFAULT_READ_PAGE_LINES` (match OpenClaw's underlying page size; confirm via upstream default, likely ~2000 lines or the chunk that fits a 32 KB block). Concatenate page text with `"\n\n"` delimiter between pages. Strip the `Use offset=N to continue` notice from all but the last page.
   - Break when: all lines consumed (full file fits), accumulated bytes ≥ cap (set `capped=True`), or 4 pages emitted.
   - If `capped`, append `"\n\n[Read output capped at {format_bytes(cap)} for this call. Use offset={next_offset} to continue.]"`.

Returns (text path) `{"success": True, "content": str, "truncated": bool, "total_lines": int, "next_offset": int | None}` or `{"success": False, "error": str}`.

**WriteFileTool schema**
- `path` (required, string)
- `content` (required, string)
- `append` (optional, bool, default false)
- `create_parents` (optional, bool, default true) — mkdir parent dir if missing (use `interface.create_dir`; retry `write_text` once on failure)

Returns `{"success": True, "bytes_written": int, "path": str}` or `{"success": False, "error": str}`.

**EditFileTool schema**
- `path` (required, string)
- `edits` (required, array of `{"oldText": str, "newText": str}`, non-empty) — each oldText must be non-empty string; newText may be empty (deletion).

Apply edits sequentially on the decoded file content. If any `oldText` not found, abort the whole batch, re-read, and return `Error: oldText not found in <path>.\nCurrent file contents (first 800 chars):\n<snippet>`. If all found, write back via `write_text` (UTF-8). On post-apply readback mismatch, return the original error unchanged (no retroactive success inference — CUA writes either succeed or raise).

Returns `{"success": True, "edits_applied": int, "path": str}` or `{"success": False, "error": str}`.

**Path policy** (`_assert_within_workspace`)
- If `workspace_root is None`: no enforcement — fall back to permissive mode matching existing MilestoneTool behavior. Log a one-line warning at tool construction.
- If `workspace_root` provided: normalize both via `ntpath.normpath` (Windows) or `posixpath.normpath`, compare case-insensitively on Windows. Reject if normalized path doesn't start with normalized root + separator. Error message: `Error: path '<path>' is outside the task workspace ('<workspace_root>').`

### Wiring: `tools.py::build_tools()`

Add two new kwargs `workspace_root: str | None = None` and `context_window_tokens: int | None = None`; append the three tools **before** the Memory tools (keeps related file-system tools adjacent; matches OpenClaw's `toolOrder`). Import at top of `tools.py`.

```python
from .tools_fs import EditFileTool, ReadFileTool, WriteFileTool

# inside build_tools, after analyze_image_tool:
read_tool = ReadFileTool(
    session.interface,
    workspace_root=workspace_root,
    context_window_tokens=context_window_tokens,
)
write_tool = WriteFileTool(session.interface, workspace_root=workspace_root)
edit_tool = EditFileTool(session.interface, workspace_root=workspace_root)
tools: list = [
    computer, milestone_tool, analyze_image_tool,
    read_tool, write_tool, edit_tool,
    memory_search, memory_get, memory_write,
]
```

### Wiring: `openclaw_agent.py::perform_task()`

Derive both `workspace_root` and `context_window_tokens` at build-tools call site. `context_window_tokens` comes from the already-constructed `ContextOverflowCallback` — **move the `build_tools()` call to after `overflow_cb` is constructed**, or pre-resolve the window via the shared helper. (The overflow callback already resolves the context window at line 249-254 with env-override support.)

```python
import os
task_tag = os.environ.get("TASK_TAG", "").strip()
if task_tag:
    root_dir = os.environ.get("REMOTE_ROOT_DIR", r"C:\Users\User\Desktop")
    category = os.environ.get("TASK_CATEGORY", "tasks")
    workspace_root = f"{root_dir}\\{category}\\{task_tag}"
else:
    workspace_root = None  # permissive

# overflow_cb already exists; reuse its resolved window
context_window_tokens = overflow_cb.context_window
```

Thread both into `build_tools(..., workspace_root=workspace_root, context_window_tokens=context_window_tokens)` (lines ~198-214 in `openclaw_agent.py`). Reorder: construct `overflow_cb` **before** `build_tools` if not already the case (trivial hoist — `overflow_cb` only depends on `self.model`, `ctx_override`, `instructions` length which can be computed from context files pre-build).

**Shell-script side**: `run_magic_tower.sh` / `run_helloworld.sh` must `export TASK_TAG=<tag>` so the Level 2 run actually exercises the policy. Check both scripts; add export if missing. Document in `progress.txt` codebase patterns.

### Tests (`tests/test_fs_tools.py`, new)

Follow the `test_analyze_image_tool.py` pattern: sync tests wrap `asyncio.run(_scenario())` and mock the interface with `MagicMock` + `AsyncMock`.

Coverage target: ≈40 unit tests, grouped:
1. **Registration & schema** — `TOOL_REGISTRY` membership, required-param declarations in schema JSON
2. **Read text happy path** — explicit limit respects slice + `truncated` flag; `total_lines` accuracy
3. **Read text adaptive paging** — no `limit` triggers page loop; cap computed from `context_window_tokens` (assert 32 KB floor, 128 KB ceiling, 10 % share at mid-range); continuation notice appended with correct next offset; fallback to 32 KB when `context_window_tokens=None`
4. **Read text errors** — missing path param, missing file (propagated from `interface.read_text`), binary content (UnicodeDecodeError), workspace-policy violation
5. **Read image happy path** — PNG/JPEG extensions trigger image branch; return dict includes `type="image"`, `data=<base64>`, `mime_type`, `text="Read image file [image/png]"`
6. **Read image sanitization** — sniffed MIME overrides extension-derived MIME when they disagree; non-image sniff (e.g. PDF magic bytes on `.png` extension) returns the `file looks like <sniffed> but was treated as <expected>` error
7. **Read image size cap** — file exceeding `max_bytes` returns formatted error with MB values (mirror `analyze_image.py:254-262`)
8. **Write happy path** — successful write, append mode, `create_parents=true` creates parent dir via `interface.create_dir`, `create_parents=false` surfaces mkdir failure
9. **Write errors** — missing required params, workspace-policy violation
10. **Edit happy path** — single edit, multiple sequential edits, empty `newText` (deletion), read→apply→write RPC sequence verified
11. **Edit errors** — oldText not found triggers mismatch hint with current-content snippet (≤800 chars, truncated suffix), empty oldText rejected, empty edits[] rejected, workspace-policy violation
12. **Path policy** — workspace_root set + inside/outside; workspace_root None + permissive; Windows-case-insensitive normalization; POSIX mode for non-Windows paths

All `session.interface.*` calls mocked as `AsyncMock`; assertions check call shape (method name + args) — NOT host `open()` — to satisfy the "host open() disallowed" acceptance criterion. Assert via `mock.interface.read_text.assert_awaited_once_with(path, encoding='utf-8')` etc.

### CUA agent.py adapter tests (`submodules/cua/libs/python/agent/tests/test_computer_agent.py`)

Add two tests mirroring the existing `TestHandleFunctionCallScreenshot` pattern (`progress.txt:31`):
- `test_handle_read_image_output_emits_sentinel_and_user_image` — when a tool result dict has `{"type": "image", "data": b64, "mime_type": mime}`, assert `function_call_output.output` is the sentinel string and a separate `user` message with an `input_image` content block was appended.
- `test_handle_non_image_tool_output_unchanged` — regression guard: dicts without `type="image"` flow through the existing JSON-dump path.

### Acceptance Criteria Mapping

| # | Criterion | How it's satisfied |
|---|-----------|--------------------|
| 1 | Lint passes | `uv run ruff check .` in CI |
| 2 | Unit tests for params, errors, session.interface call shape | `tests/test_fs_tools.py` |
| 3 | Tools route through session.interface; host `open()` disallowed | Enforced by implementation (no `open(` in `tools_fs.py`); tests assert AsyncMock call shape |
| 4 | Path policy rejects outside-workspace paths | `_assert_within_workspace`, tested in "Path policy" group |
| 5 | Level 2 VM test with 50 steps — successful read returns non-empty content | See "Verification" below |

## Verification (Three-Level Per `docs/testing-feedback-loops.md`)

**Level 1 — Static + unit:**
- `uv run ruff check . && uv run ruff format --check .`
- `uv run pytest tests/test_fs_tools.py -v` — expect ~30 passing
- `uv run pytest tests/ -v` — full harness suite stays green (≥861 tests, matches US-SUB-005 baseline)

**Level 2 — VM integration (mandatory per CLAUDE.md):**
- Task: magic_tower (`run_magic_tower.sh`). Extend the instruction (or document in the PRD) so the agent is asked to **first `read` the save file in the VM workspace** before making decisions. Alternatively, pick any task that naturally requires config inspection.
- Command (following the `VM test logging` pattern from `progress.txt:6`):
  ```bash
  # Fresh session
  TS=$(date +%Y%m%d_%H%M%S)
  mkdir -p logs/us_oc_055
  DISABLE_MAIN_COMPUTER=0 bash run_magic_tower.sh 50 > logs/us_oc_055/run_${TS}.log 2>&1
  ```
- Trajectory inspection: grep `turn_*/*_agent_response.json` for at least one `function_call` with `name == "read"` and a non-error `tool_result`. Expect `content` field with non-empty string in the result.
- Policy check: trigger one deliberate out-of-workspace read and confirm the agent receives the rejection message. (Can be done via a manual test script using the tool directly, or by watching the agent test workspace-boundary paths.)

**Level 3 — Judge pass:** Run `/judge` after Level 2 passes; iterate on any gaps it flags.

## Critical Files to Modify

| File | Change |
|------|--------|
| `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/tools_fs.py` | **NEW** — ReadFileTool, WriteFileTool, EditFileTool + helpers (MIME sniff, adaptive paging, workspace policy) |
| `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/tools.py` | Import new tools, add `workspace_root` + `context_window_tokens` kwargs, append in `build_tools()` |
| `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/__init__.py` | Export `ReadFileTool`, `WriteFileTool`, `EditFileTool` (optional, for tests) |
| `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw_agent.py` | Resolve `workspace_root` from env + hoist `overflow_cb` to pass `context_window_tokens` → `build_tools()` |
| `submodules/cua/libs/python/agent/agent/agent.py` | Small `_handle_item` patch to detect `type="image"` tool-result dicts → sentinel in function_call_output + paired `user`/`input_image` message (mirror of screenshot-collapse fix) |
| `submodules/cua/libs/python/agent/tests/test_computer_agent.py` | 2 new tests for the image-output adapter |
| `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/AGENTS.md` | Add short section on the three tools (when to use vs `analyze_image`; file-system vs GUI) |
| `run_magic_tower.sh` / `run_helloworld.sh` | Ensure `export TASK_TAG=...` is set so Level 2 exercises policy |
| `tests/test_fs_tools.py` | **NEW** — ~30 unit tests |
| `progress.txt` | Story entry + any codebase-pattern-level learnings |
| `prd.json` | Set `planFile` pointer for US-OC-055 via `/prd` (in `/ship`) |

## Existing Utilities to Reuse

- `_is_windows_path` pattern — `submodules/cua/libs/python/agent/agent/tools/milestone.py:95` + `submodules/cua/libs/python/agent/agent/tools/analyze_image.py:58`
- Sync→async executor pattern — `submodules/cua/libs/python/agent/agent/tools/analyze_image.py:149-170`
- Parent-dir extraction via `ntpath.dirname` / `posixpath.dirname` — `milestone.py:121-125`
- `BaseTool._verify_json_format_args` for param normalization — `submodules/cua/libs/python/agent/agent/tools/base.py`
- Async test pattern (sync test + `asyncio.run(_scenario())`) — `tests/test_subagent_session.py:156-157`

## Open Questions for User

1. **Workspace root resolution**: env-driven (above) vs. extending `perform_task(task_cfg=...)` signature. Env is simpler but requires the shell scripts to export TASK_TAG. Extending the signature is cleaner but breaks BaseAgent API. **Plan recommends env now, follow-up story for clean signature threading.**
2. **Image read surfacing via CUA adapter**: the `_handle_item` patch adds ~15 lines in `agent.py` and is a small but non-trivial framework change. Alternative is to keep image content inside the tool-result dict (base64 stringified into `function_call_output.output`) — works but regresses the screenshot-whale fix's invariant. **Plan recommends the adapter; if you'd rather keep US-OC-055 strictly tool-side, we can scope the adapter as a follow-up mini-story and make `read` emit a text-only stub for images in the interim.**
