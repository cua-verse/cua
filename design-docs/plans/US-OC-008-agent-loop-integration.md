# US-OC-008: Agent Loop Integration Plan

## Context

US-OC-008 is the final integration story for the OpenClaw reproduction. All B1-B5 components
(prompt builder, memory, session persistence, context management, tool registry) were wired
into `openclaw_agent.py` during their individual stories (US-OC-001 through US-OC-007).

**What's already working**: PromptBuilder, MemoryStore, Memory Tools, SessionManager,
ContextOverflowCallback, Compaction Pipeline, ToolLoggingCallback, Memory Flush.

**What's still missing** (gaps found during onboarding):
1. `build_system_prompt_report()` exists but is never called
2. Cross-run continuity: prior compaction summaries not loaded at session start
3. Category C callback verification (turns out they're auto-created by ComputerAgent — just need VM confirmation)

## OpenClaw Design Rationale

### What OpenClaw Does
OpenClaw's `runEmbeddedPiAgent` orchestrates: session lock → model resolve → workspace prep →
skills load → bootstrap injection → system prompt build → prompt report → agent loop →
compaction retries → reply shaping → persistence. The loop is serialized per session with
lifecycle events streamed to consumers.

### What We Keep and Why
- **System prompt report** — observability into prompt composition (how many chars per section,
  which files injected, tool schema sizes). Useful for debugging context budget issues.
- **Cross-run continuity** — loading prior compaction summaries so the agent has context from
  previous runs. Essential for Level 3 acceptance (two sequential runs).
- **Pre-compaction memory flush** — already wired in US-OC-005a.

### What We Drop and Why
- **Session lock / queueing** — CUA runs one task at a time; no concurrent sessions.
- **Lifecycle event streaming** — CUA uses trajectory files, not event streams.
- **Reply shaping / NO_REPLY suppression** — CUA agent doesn't deliver replies to users.
- **Skills system** — not applicable to CUA benchmark.
- **Model resolution / auth profiles** — CUA uses a single model string.

### Key Differences from OpenClaw
- CUA's ComputerAgent internally creates PromptInstructionsCallback, ImageRetentionCallback,
  and TrajectorySaverCallback from constructor params — no need to add them manually.
- Callback order: OperatorNormalizer → [our callbacks] → PromptInstructions → ImageRetention
  → TrajectorySaver. Our ContextOverflowCallback runs first in on_llm_start, which is correct
  (sees messages before image stripping = conservative token estimate).

## Implementation

### Change 1: Wire system_prompt_report (openclaw_agent.py)

After building the system prompt and before creating the agent, call `build_system_prompt_report()`
and store via `session_mgr.set_system_prompt_report()`.

```python
# After: instructions = builder.build(...)
from .openclaw import build_system_prompt_report

report = build_system_prompt_report(
    system_prompt=instructions,
    context_files=context_files,
    tool_summaries=tool_summaries,
    tools=tools,
)
session_mgr.set_system_prompt_report(report)
```

**File**: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw_agent.py` (~line 202)

### Change 2: Cross-run continuity (openclaw_agent.py)

After `session_mgr.init_session()`, check for prior compaction summaries and prepend them
to the instruction if present. This ensures the agent starts with context from previous runs.

```python
# After: session_mgr.init_session(model=self.model)
prior_summaries = session_mgr.get_compaction_summaries()
if prior_summaries:
    instruction = _create_compacted_instruction(task_description, prior_summaries)
    print(f"[CrossRun] Loaded {len(prior_summaries)} prior compaction summaries")
```

**File**: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw_agent.py` (~line 179)

### Change 3: Add import for build_system_prompt_report

Add `build_system_prompt_report` to the existing import block from `.openclaw`.

**File**: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw_agent.py` (~line 155)

## Verification

### Level 1: Lint + Unit Tests
```bash
uv run ruff check submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/ tests/
uv run pytest tests/ -x -q
```

### Level 2: VM Test (50 steps)
```bash
bash run_magic_tower.sh 50
```
Verify:
- Agent runs with all components active
- `openclaw_sessions/<task_id>/state.json` exists with token totals > 0
- `openclaw_sessions/<task_id>/transcript.jsonl` has 10+ entries
- `openclaw_memory/` has .md files with game-relevant content
- `system_prompt_report` present in state.json

### Level 3: Two Sequential Runs
```bash
bash run_magic_tower.sh 15  # Run 1 (short)
bash run_magic_tower.sh 15  # Run 2 — should load Run 1's data
```
Verify:
- state.json shows updated step counts
- transcript.jsonl has 2 session headers
- If Run 1 triggered compaction, Run 2's instruction includes prior summaries
