<!-- Last updated: 2026-04-24 (US-OC-031) -->
# OpenClaw → AgentHLE Tool Migration Audit

Systematic review of OpenClaw's tool surface to identify which tools are worth porting to the AgentHLE / CUA agent harness, with classification, migration complexity, and priority.

Acceptance criteria from US-OC-031:
- (a) what it does, (b) relevance to CUA computer-use, (c) migration complexity, (d) priority
- covers at least: file tools, shell tools, web browsing, memory tools, code analysis, messaging
- for each `migrate`/`adapt`, the OpenClaw source file and the CUA integration point

## Summary

- **28** distinct OpenClaw tools inventoried across 8 categories.
- **Classification totals:** 3 migrate, 6 adapt, 16 skip, 3 already-have.
- **Top 3 recommended next migrations:**
  1. **Remote-VM `read` / `write` / `edit`** (P1, adapt, medium) — unlocks task inspection without burning screenshots; largest single capability gap.
  2. **`web_search` + `web_fetch`** (P1, migrate, low) — task-agnostic reasoning scaffolding; no external deps we lack.
  3. **`update_plan`** (P2, migrate, low) — lightweight plan-tracking BaseTool with demonstrable per-task value; low effort, high readability of trajectories.

## Methodology

| Aspect | Definition |
|---|---|
| **Source of truth (OpenClaw)** | `../openclaw/src/agents/openclaw-tools.ts`, `../openclaw/src/agents/pi-tools.ts`, and files under `../openclaw/src/agents/tools/` |
| **Source of truth (harness)** | `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/tools.py::build_tools()` |
| **Relevance (H/M/L)** | H = used by ≥50% of plausible computer-use tasks; M = used by specific task classes; L = edge-case only |
| **Complexity (low/medium/high)** | low = pure BaseTool port; medium = needs remote-VM adaptation or a new helper; high = requires infra we don't have |
| **Classification** | `migrate` = port near-verbatim as a new BaseTool · `adapt` = keep the API shape but retarget to CUA (remote VM, CUA patterns) · `skip` = do not port (with reason) · `already-have` = harness already covers this |
| **CUA integration point** | For migrate/adapt, the specific file or module in the harness where the new tool lands |

### Prompt-builder obligation (meta-finding)

OpenClaw's `system-prompt.ts` emits **per-tool operational prose blocks** alongside the tool listing — e.g. polling guardrails for `exec`/`process`/`cron`; `update_plan`'s "multi-step only, one `in_progress` at a time"; `sessions_spawn` routing rules. Our `prompt.py` only emits such blocks for the Delegation section.

**Rule to apply when landing a migrated tool:** if OpenClaw's `system-prompt.ts` has a dedicated operational block for the tool, the follow-up story must add a matching `prompt.py::_build_<tool>()` method gated on `"<tool>" in tool_summaries`, mirroring `_build_delegation()`. If OpenClaw has no prose for that tool, the `description` property on the BaseTool subclass is sufficient.

Tools that do need a prompt-builder section if migrated: `exec`, `process`, `update_plan`, `apply_patch`.
Tools that don't: `read`, `write`, `edit`, `web_search`, `web_fetch`, `pdf`.

## Category 1 — Coding / Filesystem

### `read` — **adapt** (H, medium, P1)
- **What it does:** read file or directory contents with offset + limit; image sanitization of returned content.
- **OpenClaw source:** `../openclaw/src/agents/pi-tools.read.ts` (via `createSandboxedReadTool` / `createHostWorkspaceReadTool`; underlying `readTool` from `@mariozechner/pi-coding-agent`).
- **CUA integration point:** new `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/tools_fs.py`; must route through `session.interface` (computer-server RPC) for remote Windows reads — **not** host `open()`. Mirror `AnalyzeImageTool`'s remote-path detection pattern (it already reads from the VM via `session.interface.get_file_data`).
- **Rationale:** tasks often need to inspect game saves, configs, output files without taking a screenshot; direct file reads are cheaper in tokens and deterministic.
- **Prompt-builder obligation:** none (schema-only is fine).

### `write` — **adapt** (H, medium, P1)
- **What it does:** create or overwrite a file in the workspace; append mode.
- **OpenClaw source:** `../openclaw/src/agents/pi-tools.read.ts::createHostWorkspaceWriteTool` / `createSandboxedWriteTool`.
- **CUA integration point:** `tools_fs.py`; write via `session.interface` — verify the CUA computer-server exposes a file-write RPC or add one. Must enforce a target-path policy (VM workspace only).
- **Prompt-builder obligation:** none.

### `edit` — **adapt** (M, medium, P1)
- **What it does:** replace a line range or string span in an existing file.
- **OpenClaw source:** `../openclaw/src/agents/pi-tools.read.ts::createHostWorkspaceEditTool` / `createSandboxedEditTool`.
- **CUA integration point:** `tools_fs.py`; builds on top of `read` + `write` RPCs. Consider porting OpenClaw's recovery wrapper (`wrapToolParamValidation`) for better error messages on malformed ranges.
- **Prompt-builder obligation:** none.

### `apply_patch` — **adapt** (L, medium, P3)
- **What it does:** apply a Begin-Patch / End-Patch block with add/delete/update/move directives.
- **OpenClaw source:** `../openclaw/src/agents/apply-patch.ts`.
- **CUA integration point:** `tools_fs.py`; gated by model provider (OpenAI only in OpenClaw). Relies on `read`/`write` being in place first.
- **Prompt-builder obligation:** yes — add `_build_apply_patch()` with patch format rules.

## Category 2 — Shell / Process

### `exec` — **adapt** (H, medium, P2)
- **What it does:** run a bash command with cwd, timeout, env overrides, approval gates.
- **OpenClaw source:** `../openclaw/src/agents/bash-tools.ts` (schema in `bash-tools.schemas.ts`).
- **CUA integration point:** new `tools_shell.py`; must run **inside the Windows VM** (PowerShell or `cmd`) via `session.interface.run_command` (confirm RPC exists in computer-server; if not, add it). Keep OpenClaw's approval/safe-bin concepts for reference but simplify — benchmark tasks aren't subject to the same policy surface.
- **Why valuable:** non-GUI shell access is a force multiplier for tasks that don't need pixels (file searches, launching apps via `start`, reading JSON output).
- **Prompt-builder obligation:** yes — add `_build_exec()` with polling/looping guardrails.

### `process` — **adapt** (L, high, P4)
- **What it does:** list/kill/monitor background processes spawned via `exec --background`.
- **OpenClaw source:** `../openclaw/src/agents/bash-tools.ts` (process registry side).
- **CUA integration point:** `tools_shell.py`; requires a per-task process registry in the harness. Defer until `exec` background mode has a real use case.
- **Prompt-builder obligation:** yes if landed.

## Category 3 — Communication / Sessions

See `docs/openclaw-subagent-fidelity.md` for the full per-tool breakdown. Summary:

- **`subagents`** — **already-have** (harness `SubagentsTool`; 95% parity, minor gaps queued as a P4 follow-up).
- **`sessions_spawn`** — **already-have** (harness `DelegateGeneralTool` + `DelegateGUITool`; ~70% semantic parity, remaining surface area is deliberately dropped due to architectural mismatch — no ACP, no persistent sessions, no agent roster).
- **`sessions_history`** — **migrate** (L–M, low, P4). Thin read-only wrapper over `<parent_session_dir>/subagents/<run_id>/transcript.jsonl`. Lets the main agent inspect a completed subagent's full reasoning rather than only the `[Subagent Result]` drain summary. **CUA integration point:** new `SubagentHistoryTool` in `subagent_tools.py`, gated by the same `registry` kwarg as the delegation tools.
- **`sessions_yield`** — **skip**. Redundant with `_drain_completions` push model.
- **`sessions_list`** — **skip**. Single-agent context; `subagents action=list` covers it.
- **`sessions_send`** — **skip**. Subagents are ephemeral; re-delegating is cheaper.
- **`session_status`** — **skip**. Main agent already knows its own state; subagent config is immutable after spawn.
- **`agents_list`** — **skip**. No named-agent roster in CUA.
- **`message`** — **skip**. Requires the OpenClaw channel plugin system (Slack / Telegram / WhatsApp); benchmarks have no channel concept.

## Category 4 — Media Generation

All **skip** — out of scope for computer-use benchmarks.

- `image_generate` — `../openclaw/src/agents/tools/image-generate-tool.ts`. Skip.
- `video_generate` — `video-generate-tool.ts`. Skip.
- `music_generate` — `music-generate-tool.ts`. Skip.
- `tts` — `tts-tool.ts`. Skip.

No task in the current PRD requires generating media inside the VM run.

## Category 5 — Web

### `web_search` — **migrate** (H, low, P1)
- **What it does:** query a web search provider (Brave / DuckDuckGo / Bing depending on config) and return ranked results.
- **OpenClaw source:** `../openclaw/src/agents/tools/web-tools.ts::createWebSearchTool` (delegates to `web-search.ts`).
- **CUA integration point:** new `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/tools_web.py`; `WebSearchTool(BaseTool)` that wraps `httpx` (or `litellm`'s search if available) with a configurable provider. Task-agnostic — does not touch the VM.
- **Rationale:** frequently useful for tasks where the agent needs external context (game walkthroughs, API docs, error messages) that isn't derivable from the VM.
- **Prompt-builder obligation:** none.

### `web_fetch` — **migrate** (M, low, P1)
- **What it does:** fetch a URL, strip chrome, return Readability-extracted markdown or text.
- **OpenClaw source:** `../openclaw/src/agents/tools/web-tools.ts::createWebFetchTool` (delegates to `web-fetch.ts`).
- **CUA integration point:** `tools_web.py`; `WebFetchTool(BaseTool)` using `httpx` + `readability-lxml` (or `trafilatura`). Must include SSRF guard (reject private IPs / localhost / link-local) — OpenClaw's `web-guarded-fetch.ts` is the reference.
- **Prompt-builder obligation:** none.

## Category 6 — Automation / Planning

### `update_plan` — **migrate** (M, low, P2)
- **What it does:** track a multi-step plan as `[{step, status: pending|in_progress|completed}, ...]`; the tool call is a side-effect-free notification that the harness persists + surfaces in logs.
- **OpenClaw source:** `../openclaw/src/agents/tools/update-plan-tool.ts`.
- **CUA integration point:** new `UpdatePlanTool(BaseTool)` in `tools.py` (or a new `planning.py`); stores current plan on `MemoryStore` (append to `TASK_MEMORY.md` or a dedicated `PLAN.md`). No external deps.
- **Prompt-builder obligation:** yes — add `_build_update_plan()` with OpenClaw's guidance: "use only for non-trivial multi-step work, keep exactly one `in_progress` step, don't restate the whole plan every turn."

### `cron` — **skip**
- CUA tasks are one-shot runs; no persistent scheduler exists to fire wake-ups, and benchmark determinism argues against wall-clock triggers.
- **OpenClaw source (for reference):** `../openclaw/src/agents/tools/cron-tool.ts`.

## Category 7 — Workspace / UX

### `canvas` — **skip**
- Tied to the OpenClaw web-chat node-canvas renderer. No analog in CUA.
- **OpenClaw source:** `../openclaw/src/agents/tools/canvas-tool.ts`.

### `nodes` — **skip**
- OpenClaw device pairing + node commands (camera, location, notifications). CUA has one remote Windows VM, not a device fleet.
- **OpenClaw source:** `../openclaw/src/agents/tools/nodes-tool.ts`.

### `image` (workspace image reader) — **already-have**
- Covered by `AnalyzeImageTool` (US-OC-033) — same role: send image to VLM with a prompt and get text back. Our version reads from the remote VM or local path.
- **Harness tool:** `submodules/cua/libs/python/agent/agent/tools/analyze_image.py`.

### `pdf` — **adapt** (M, medium, P3)
- **What it does:** send a PDF to an Anthropic / Gemini native PDF endpoint (or extract + pass pages to a VLM) with a prompt; returns text analysis.
- **OpenClaw source:** `../openclaw/src/agents/tools/pdf-tool.ts`.
- **CUA integration point:** new `AnalyzePdfTool(BaseTool)` in the CUA agent tools (alongside `AnalyzeImageTool`); should read PDFs from the VM via `session.interface`, then delegate to `litellm` with the provider's native PDF support (matching `AnalyzeImageTool`'s dual-path approach).
- **Why adapt not migrate:** file-path resolution needs the same remote-VM treatment as `read`; also need to choose which PDF extractor to ship with.
- **Prompt-builder obligation:** none.

## Category 8 — Admin

### `gateway` — **skip**
- Owner-only admin tool for reading / patching OpenClaw runtime config and triggering restarts. No gateway in CUA, no admin surface.
- **OpenClaw source:** `../openclaw/src/agents/tools/gateway-tool.ts`.

## Cross-cutting: Memory tools

OpenClaw does not expose `memory_*` tools in the same shape as our harness; its memory layer is internal to `auto-reply/reply/agent-runner-memory.ts` and `agent-state/memory`. Our `MemorySearchTool` / `MemoryGetTool` / `MemoryWriteTool` (`memory.py`) are CUA-native.

- **Classification:** already-have, no OpenClaw tool to migrate here.

## Prioritized Backlog

The table below is the prioritized list of follow-up stories implied by this audit. Each entry should become (or update) a PRD user story.

| # | Priority | Scope | Classification | Complexity | Prompt change? | Notes |
|---|---|---|---|---|---|---|
| 1 | P1 | Remote-VM `read` + `write` + `edit` | adapt | medium | no | single story covering all three; reuses `session.interface` pattern from `AnalyzeImageTool` |
| 2 | P1 | `web_search` + `web_fetch` | migrate | low | no | task-agnostic; SSRF guard for fetch |
| 3 | P2 | Remote-VM `exec` | adapt | medium | **yes** | non-GUI shell inside the VM; add `_build_exec()` |
| 4 | P2 | `update_plan` | migrate | low | **yes** | add `_build_update_plan()` |
| 5 | P3 | `apply_patch` | adapt | medium | **yes** | OpenAI-gated; after remote file I/O lands |
| 6 | P3 | `pdf` analysis | adapt | medium | no | mirrors `AnalyzeImageTool` shape |
| 7 | P4 | `process` (bg process mgmt) | adapt | high | yes | gate on `exec` background-mode demand |
| 8 | P4 | `sessions_history` | migrate | low | no | thin wrapper over on-disk subagent transcripts (see `docs/openclaw-subagent-fidelity.md`) |
| 9 | P4 | `subagents` parity | enhancement | low | no | `recentMinutes` filter, `all`/`*` wildcard kill, cascade-kill counts (steer cap is already at parity — both sides 4K) |

## Existing PRD story alignment

- **US-OC-029 (Tool: Subagent Delegation, `passes: false`)** — already superseded by the landed US-SUB-* work. Recommend marking `passes: true` with a note pointing to US-SUB-005, or converting into the `sessions_history` story.
- **US-OC-030 (Tool: Visual Analysis, `passes: false`)** — superseded by US-OC-033 (`AnalyzeImageTool`). Recommend marking `passes: true`.
- **US-OC-053 (Tool Policy Resolver, `passes: false`)** — orthogonal to this audit; remains the long-horizon story that generalizes `disable_*` kwargs into a declarative policy. New migrated tools should still hook into the same gating pattern (see `tools.py:144-246`).
