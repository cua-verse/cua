<!-- Last updated: 2026-04-24 (extracted from US-OC-031 planning appendix) -->
# OpenClaw Subagent & Sessions Tool Fidelity Report

> **Handoff note.** This report was produced during US-OC-031 planning. It's intended for a separate agent to drive follow-up stories for the minor `subagents` parity gaps and a potential `sessions_history` migration. The US-OC-031 audit itself only cites the conclusions here (under Category 3 of `docs/tool-migration-audit.md`) — it does not act on them.

Sources:
- `../openclaw/src/agents/tools/subagents-tool.ts`
- `../openclaw/src/agents/tools/sessions-spawn-tool.ts`
- `../openclaw/src/agents/tools/sessions-{yield,send,list,history}-tool.ts`, `session-status-tool.ts`, `agents-list-tool.ts`
- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/subagent_tools.py`
- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/subagent_registry.py`
- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/subagent_session.py`
- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/agent_loop.py` (`_drain_completions`)

## 1. `subagents` fidelity — ~95% parity

| Aspect | OpenClaw (`subagents-tool.ts`) | Harness (`SubagentsTool`) | Gap |
|---|---|---|---|
| Action coverage | `list`, `kill`, `steer` | `list`, `kill`, `steer` | ✓ parity |
| `list` filter | `recentMinutes` (1–60) | none (shows all) | Minor — trivial to add |
| `kill` target resolution | `run_id`, `label`, prefix, `"all"`, `"*"` | `run_id`, `label`, prefix, `"last"` | Missing wildcard kill; has `"last"` shorthand |
| `steer` message cap | 4 000 chars (`MAX_STEER_MESSAGE_CHARS` in `subagent-control.ts`) | 4 000 chars (same constant name in `subagent_tools.py`) | ✓ parity |
| Return shape | `cascadeKilled`, `cascadeLabels`, `callerSessionKey`, `callerIsSubagent`, `sessionId` (on steer) | minimal: `status`, `target`, `killed`/`steered`, `reason` | Harness omits cascade + caller identity — not material for single-agent setup |

**Recommended follow-up story (P4):** "Subagents tool parity — add `recentMinutes`, `all`/`*` kill wildcard, surface cascade-kill counts in return."

## 2. `sessions_spawn` fidelity — ~70% semantic parity (deliberate)

OpenClaw's `sessions_spawn` is a generic multi-runtime entry point. Our harness splits it into **two role-based tools** — `DelegateGeneralTool` and `DelegateGUITool` — with simpler surface area.

**OpenClaw parameters:** `task`, `label`, `runtime ∈ {subagent, acp}`, `agentId`, `model`, `thinking`, `cwd`, `runTimeoutSeconds`, `thread`, `mode ∈ {run, session}`, `cleanup ∈ {delete, keep}`, `sandbox ∈ {inherit, require}`, `streamTo ∈ {parent}`, `lightContext`, `attachments[]`, `attachAs.mountPath`, `resumeSessionId` (ACP only).

**Harness parameters (general):** `task`, `label`, `model`, `max_steps`, `screenshot_paths[]`.
**Harness parameters (gui):** `instruction`, `label`, `model`, `max_steps`.

**Deliberately dropped — architectural mismatch (not bugs):**
- `runtime=acp` — no ACP runtime in CUA.
- `agentId` — no named-agent roster (we use roles: general vs GUI).
- `mode=run|session` — no persistent multi-session graph.
- `thread` — no cross-agent threading.
- `streamTo` — no gateway streaming; we use `_drain_completions` push model.
- `sandbox` — fixed single-VM environment.
- `cleanup`, `resumeSessionId` — no session resurrection semantics.
- `cwd`, `runTimeoutSeconds` explicit override — fixed/implicit.
- `lightContext`, `attachments[]` — replaced by simpler `screenshot_paths[]` on general.

**Return shape:** harness surfaces only `run_id` + `note`; OpenClaw adds `childSessionKey`, `mode`, `cleanup` metadata. Acceptable because `run_id` is the sole handle we need.

**Verdict:** core capability ("spawn async worker, get results back") is fully preserved. The delta is scoped to what CUA's runtime actually supports.

## 3. Remaining six session tools — usefulness verdicts

| Tool | Useful here? | Rationale |
|---|---|---|
| `sessions_yield` | No | Redundant with `_drain_completions` (`agent_loop.py`) — subagent results are push-delivered as `[Subagent Result]` user messages every turn. An explicit yield contradicts the push model. |
| `sessions_list` | No | Single-agent, single-task context. `subagents action=list` already shows every subagent that exists. No cross-agent transparency needed. |
| **`sessions_history`** | **Yes — weak candidate (P4)** | Thin read-only wrapper over the existing on-disk transcript at `<parent_session_dir>/subagents/<run_id>/transcript.jsonl`. Would let the main agent inspect a completed subagent's *full* reasoning + tool calls rather than just the drain summary. Useful for "why did it do X?" follow-ups. Non-blocking. |
| `sessions_send` | No | Subagents are ephemeral — teardown after `complete`/`fail`/`kill`. No live session to message. A follow-up question is cheaper as a fresh `delegate_general` call. |
| `session_status` | No | Main agent has direct access to its own model + token state via the overflow callback; subagent config is immutable post-spawn. |
| `agents_list` | No | CUA uses role-based delegation (general vs GUI), not a named-agent roster. Architectural mismatch — nothing to list. |

## 4. Suggested follow-up tickets

1. **US-OC-05X — Subagents tool parity.** Add `recentMinutes` to list, `"all"/"*"` wildcard to kill, include `cascade_killed` + `cascade_labels` in kill return. Est: S. Touches `subagent_tools.py::SubagentsTool`, `subagent_registry.py` (cascade enumeration).
2. **US-OC-05Y — `sessions_history` read-only tool.** BaseTool that takes a `run_id` and returns the subagent's transcript as typed blocks (or truncated). Read-only; gated by registry presence just like the delegation tools. Est: S. Touches `subagent_tools.py`, `tools.py::build_tools()`, `prompt.py::_build_delegation()` (add a bullet if the tool is present).
