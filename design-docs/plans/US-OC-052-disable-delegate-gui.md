# US-OC-052 — Tool-Filter-Driven Prompt Assembly + `disable_delegate_gui`

## Context

The harness had one runtime toggle for the tool surface (`disable_main_computer`) and a static `openclaw/AGENTS.md` loaded verbatim into the system prompt. Two problems:

1. No matching toggle to disable `delegate_gui`, so there was no way to force the main agent to handle GUI work directly.
2. AGENTS.md conflated (a) static workspace prose ("this is home, save milestones, use memory") with (b) feature-gated guidance that names specific tools ("delegate_gui takes over the VM…", "Be precise with mouse clicks…"). When flags changed, the prompt described tools the agent couldn't actually call.

OpenClaw reference — verified by reading `../openclaw/src/agents/system-prompt.ts` + `workspace.ts`:

1. **Tool filter first.** `filterToolsByPolicy` (`pi-tools.policy.ts:121-126`) reduces the tool list before it reaches the model.
2. **Tools section is built from the post-filter list.** `system-prompt.ts:555-561` — absence is the signal; disabled tools never appear.
3. **Feature-gated prose lives in code, not markdown.** `buildAgentSystemPrompt` is a 1004-line TypeScript function with ~37 conditional branches. It does not load or template any markdown file.
4. **On-disk workspace `AGENTS.md` is user-authored prose.** `workspace.ts:26` reads it verbatim from the operator's workspace dir and injects it as a bootstrap context file. OpenClaw never templates or edits it.

## What was delivered

1. **Tool-list filter.** `build_tools(..., disable_delegate_gui=False)` in `openclaw/tools.py`. When `True`, `DelegateGUITool` is skipped in `tools.extend(...)`; `DelegateGeneralTool` and `SubagentsTool` stay.
2. **Feature-gated prose in `PromptBuilder`.** Two new conditional section builders following the existing `_build_memory` pattern:
   - `_build_delegation(tool_summaries)` — emitted when any of `delegate_general` / `delegate_gui` / `subagents` are registered. Iterates the present subset. Steer-action scope narrows to "general" when `delegate_gui` is absent.
   - `_build_gui_interaction()` — emitted when `main_computer_interactive=True`. Holds the direct-click/keyboard bullets.
   `PromptBuilder.build()` gained `main_computer_interactive: bool = True` kwarg. `PromptConfig` gained `delegation` and `gui_interaction` toggles. Section order: identity → tools → memory → delegation → gui_interaction → time → project_context.
3. **AGENTS.md trimmed.** Removed the entire Delegation section and the two direct-GUI bullets from General Behavior. The content migrated into PromptBuilder. AGENTS.md stays on disk as flag-independent workspace prose (~85 lines).
4. **Conflict guard.** `OpenClawAgent.__init__` raises `ValueError` when both `disable_main_computer` and `disable_delegate_gui` are `True` — the agent would have no way to interact with the VM.
5. **`SubagentsTool.description` cleanup.** The per-tool description used to hard-code "general or GUI" in the steer-action narrative, which leaked into the Tools section of the system prompt even when `delegate_gui` was absent. The phrasing is now tool-type-agnostic ("sends a follow-up message into a running subagent"); the detailed scope lives in the PromptBuilder Delegation section which has proper control flow.
6. **CLI + env plumbing.** `--disable-delegate-gui` CLI flag in `batch/solver.py`; `DISABLE_DELEGATE_GUI=1` env block in `run_magic_tower.sh`.

## Files changed

- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/prompt.py` — `_build_delegation`, `_build_gui_interaction`, `PromptConfig` + `build()` extensions
- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/tools.py` — `disable_delegate_gui` kwarg; conditional `tools.extend(...)`
- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/AGENTS.md` — trimmed
- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/subagent_tools.py` — `SubagentsTool.description` cleanup
- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw_agent.py` — flag read, conflict guard, stdout confirmation, `main_computer_interactive` threading
- `submodules/cua/libs/cua-bench/cua_bench/batch/solver.py` — `--disable-delegate-gui` arg
- `run_magic_tower.sh` — `DISABLE_DELEGATE_GUI` env block
- `tests/test_prompt_builder.py` — `TestDelegationSection`, `TestGuiInteractionSection`, `TestFullPromptIntegration`
- `tests/test_openclaw_tools.py` — `test_disable_delegate_gui_*` parallels of `test_disable_main_computer_*`
- `tests/test_openclaw_agent_prompt.py` — `TestDisableFlags` (end-to-end absence + conflict guard)

## Design rationale vs OpenClaw

**What OpenClaw does.** Two layers: (a) `filterToolsByPolicy` cuts the tool list before the model sees it; (b) the Tools section of `buildAgentSystemPrompt` is constructed from the post-filter list. Feature-gated guidance lives in TypeScript control flow, not in markdown. User-authored workspace `AGENTS.md` is separate — loaded verbatim, never templated.

**What we kept.**
- Tool-filter-first pattern (our `build_tools` kwargs are the equivalent of small deny lists).
- "Absence is the signal" — disabled tools are not mentioned anywhere in the prompt. No "delegate_gui is disabled" narration.
- Workspace AGENTS.md as static user-authored prose, injected verbatim.
- Feature-gated prose in code (`PromptBuilder`) mirroring `buildAgentSystemPrompt`.

**What we dropped.**
- No runtime error when the agent tries to call an absent tool (unlike `_RestrictedComputerHandler` for `disable_main_computer`). The function-calling API rejects unknown tool calls natively, and we don't want to narrate the absence. The runtime error path is only needed when a tool is *partially* restricted (like the main computer keeping `screenshot`/`wait`).
- No template language in AGENTS.md (no `<<TOKEN>>`, `{placeholder}`, or marker syntax). Considered, rejected in favor of code-driven conditional sections.

**Key differences from OpenClaw.**
- `disable_main_computer` keeps the `computer` tool visible in the schema but swaps its handler with `_RestrictedComputerHandler` (allows `screenshot`+`wait`, errors on everything else). OpenClaw would drop the tool entirely, but we need `screenshot` and `wait` to keep the observation loop alive when GUI is delegated. That's why `main_computer_interactive: bool` is a separate PromptBuilder kwarg — it can't be inferred from the tool list alone.
- We have two boolean flags rather than a full policy system (allow/deny/profile/byProvider). Sufficient for today's single-agent, single-config use case; generalization tracked as **US-OC-053**.

## Acceptance criteria

- Level 1: `uv run ruff check` passes on all touched files.
- Level 1: `uv run pytest tests/test_prompt_builder.py tests/test_openclaw_tools.py tests/test_openclaw_agent_prompt.py tests/test_subagent_tools.py` passes.
- Level 1: Full `uv run pytest tests/` passes (modulo one pre-existing `test_subagent_tools` failure unrelated to this work — commit `a9b0eb79` in the submodule).
- Level 2: `DISABLE_DELEGATE_GUI=1 CONTEXT_WINDOW_OVERRIDE=50000 bash run_magic_tower.sh 20` completes with `MAX_STEPS_EXCEEDED`; `trycua/cua-bench/mota_24_easy/task_0_agent_logs/trajectories/.../turn_000/0000_api_start.json` contains `computer`, memory tools, `delegate_general`, `subagents` but not `delegate_gui` in `kwargs.tools[].function.name`; the system prompt text contains no `delegate_gui` substring and no "general or GUI" phrase.
- Level 2: Default run (no flag) continues to include `delegate_gui` in the tool list and `delegate_gui(instruction` in the prompt.
- Conflict guard: constructing `OpenClawAgent(disable_main_computer=True, disable_delegate_gui=True)` raises `ValueError`.

## Status

Implementation complete and verified 2026-04-22. Level 1 + Level 2 both passed. Not yet committed — awaiting `/ship`.
