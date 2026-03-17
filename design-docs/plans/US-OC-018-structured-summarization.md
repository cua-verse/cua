# US-OC-018: Structured Summarization Format

## Context

US-OC-006 implemented the compaction pipeline but wrote `SUMMARIZATION_SYSTEM_PROMPT` and the user-turn prompt from scratch, because the actual summarization logic in OpenClaw is delegated to `generateSummary` from the closed-source npm package `@mariozechner/pi-coding-agent`. That package's source was not inspected during US-OC-006.

The package source was later retrieved from the npm registry (`@mariozechner/pi-coding-agent@0.58.1`, GitHub: https://github.com/badlogic/pi-mono). The actual prompts differ significantly from what we implemented. This story closes that gap.

**Golden reference**: `@mariozechner/pi-coding-agent` npm package — `dist/core/compaction/compaction.js` (prompts) and `dist/utils.js` (`SUMMARIZATION_SYSTEM_PROMPT`). OpenClaw depends on this as a versioned npm package (`package.json`: `"@mariozechner/pi-coding-agent": "0.55.3"`), not a submodule.

## OpenClaw Design Rationale

### What OpenClaw Does

OpenClaw's `generateSummary` (`@mariozechner/pi-coding-agent/dist/core/compaction/compaction.js`) uses three distinct prompt strings:

**`SUMMARIZATION_SYSTEM_PROMPT`** (system role):
```
You are a context summarization assistant. Your task is to read a conversation between a user and an AI coding assistant, then produce a structured summary following the exact format specified.

Do NOT continue the conversation. Do NOT respond to any questions in the conversation. ONLY output the structured summary.
```

**`SUMMARIZATION_PROMPT`** (user turn — first summary, no prior summary):
```
The messages above are a conversation to summarize. Create a structured context checkpoint summary that another LLM will use to continue the work.

Use this EXACT format:

## Goal
[What is the user trying to accomplish?]

## Constraints & Preferences
- [constraints, preferences, requirements]

## Progress
### Done
- [x] [Completed tasks]

### In Progress
- [ ] [Current work]

### Blocked
- [Issues preventing progress]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [Ordered list of what should happen next]

## Critical Context
- [Data, examples, or references needed to continue]
```

**`UPDATE_SUMMARIZATION_PROMPT`** (user turn — update when prior summary exists):
```
The messages above are NEW conversation messages to incorporate into the existing summary provided in <previous-summary> tags.

Update the existing structured summary with new information. RULES:
- PRESERVE all existing information from the previous summary
- ADD new progress, decisions, and context from the new messages
- UPDATE the Progress section: move items from "In Progress" to "Done" when completed
- UPDATE "Next Steps" based on what was accomplished
- PRESERVE exact file paths, function names, and error messages
- If something is no longer relevant, you may remove it
[same markdown format as initial prompt]
```

The conversation is serialized to plain text and wrapped in `<conversation>` tags. The prior summary (if any) is wrapped in `<previous-summary>` tags. Both are placed in the **user** message, not the system message. The model is instructed not to continue the conversation.

When a model supports reasoning, `generateSummary` uses `reasoning: "high"`.

### What We Currently Do (US-OC-006)

| Aspect | OpenClaw | Ours (US-OC-006) |
|--------|----------|------------------|
| System prompt | Strict: "do not continue, ONLY output structured summary" | Loose: "produce a concise but complete summary, focus on actions/decisions/current state" |
| Output format | Structured markdown with fixed sections (Goal, Constraints, Progress, Decisions, Next Steps, Critical Context) | Freeform prose |
| Initial vs update prompts | Two distinct prompts with explicit update rules (preserve/add/move items) | Single prompt with `previous_summary` prepended as a header |
| Reasoning | `reasoning: "high"` when model supports it | `temperature=0.3`, no reasoning |
| Message wrapping | `<conversation>` and `<previous-summary>` XML tags | Headers in plain text (`## Previous context summary`) |

### What We Keep and Why

- **Iterative rolling summarization** — feed prior summary as context to each subsequent chunk; same pattern, different prompt style
- **litellm.acompletion** — unchanged; OpenClaw uses its own provider routing, we use litellm
- **Three-tier fallback** — the fallback tiers are independent of the prompt format

### What We Change and Why

1. **System prompt** — Adopt OpenClaw's strict system prompt verbatim. Prevents the model from treating the summary prompt as a conversation continuation, which is a real failure mode in long sessions.

2. **Two distinct user-turn prompts** — Adopt `SUMMARIZATION_PROMPT` for first summaries, `UPDATE_SUMMARIZATION_PROMPT` for updates. The update prompt's explicit rules (preserve/add/move Progress items) are valuable for multi-compaction sessions where earlier summaries must not be silently overwritten.

3. **Structured output format** — Adopt the section structure. For CUA/game tasks, the relevant sections map naturally: Goal → task objective, Progress → milestone completions, Next Steps → immediate actions, Critical Context → game state (coordinates, inventory, current position).

4. **XML tag wrapping** — Adopt `<conversation>` and `<previous-summary>` tags for clear boundary marking.

### What We Drop and Why

- **`reasoning: "high"`** — Skip for now. Reasoning models cost more and require different litellm parameters. The structured prompt format is the higher-value change. Can be revisited as a config option.

### Key Differences from OpenClaw

- **"Coding assistant" framing in system prompt** — OpenClaw's system prompt says "AI coding assistant". For CUA game tasks, this framing is slightly off but harmless; the structured sections work regardless.
- **No `customInstructions` for identifier policy** — OpenClaw's `buildCompactionSummarizationInstructions()` conditionally appends identifier preservation based on `identifierPolicy` enum. We always include identifier preservation in the system prompt. Simpler and sufficient for CUA.

## Implementation Plan

### File: `agents/openclaw/context.py`

Replace the three prompt constants:

```python
SUMMARIZATION_SYSTEM_PROMPT = (
    "You are a context summarization assistant. Your task is to read a conversation "
    "between a user and an AI coding assistant, then produce a structured summary "
    "following the exact format specified.\n\n"
    "Do NOT continue the conversation. Do NOT respond to any questions in the "
    "conversation. ONLY output the structured summary."
)

SUMMARIZATION_PROMPT = """\
The messages above are a conversation to summarize. Create a structured context \
checkpoint summary that another LLM will use to continue the work.

Use this EXACT format:

## Goal
[What is the user trying to accomplish? Can be multiple items if the session covers different tasks.]

## Constraints & Preferences
- [Any constraints, preferences, or requirements mentioned]
- [Or "(none)" if none were mentioned]

## Progress
### Done
- [x] [Completed tasks/changes]

### In Progress
- [ ] [Current work]

### Blocked
- [Issues preventing progress, if any]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [Ordered list of what should happen next]

## Critical Context
- [Any data, examples, or references needed to continue]
- [Or "(none)" if not applicable]

Keep each section concise. Preserve exact file paths, function names, identifiers, \
and error messages.\
"""

UPDATE_SUMMARIZATION_PROMPT = """\
The messages above are NEW conversation messages to incorporate into the existing \
summary provided in <previous-summary> tags.

Update the existing structured summary with new information. RULES:
- PRESERVE all existing information from the previous summary
- ADD new progress, decisions, and context from the new messages
- UPDATE the Progress section: move items from "In Progress" to "Done" when completed
- UPDATE "Next Steps" based on what was accomplished
- PRESERVE exact file paths, function names, identifiers, and error messages
- If something is no longer relevant, you may remove it

Use the same structured format as the original summary.\
"""
```

Update `summarize_chunk()` to:
- Use `UPDATE_SUMMARIZATION_PROMPT` when `previous_summary` is set, `SUMMARIZATION_PROMPT` otherwise
- Wrap conversation text in `<conversation>` tags
- Wrap prior summary in `<previous-summary>` tags
- Place both in the user message (not system), matching OpenClaw's layout

Remove `MERGE_SUMMARIES_INSTRUCTIONS` — it was a workaround for unstructured freeform summaries; the structured format makes it unnecessary.

### Tests

Update `tests/test_openclaw_compaction.py`:
- Verify system prompt text matches new `SUMMARIZATION_SYSTEM_PROMPT`
- Verify `SUMMARIZATION_PROMPT` is used when no `previous_summary`
- Verify `UPDATE_SUMMARIZATION_PROMPT` is used when `previous_summary` is set
- Verify `<conversation>` tag wrapping in user message
- Verify `<previous-summary>` tag wrapping when prior summary present

## Acceptance Criteria

- Level 1: Lint passes (`uv run ruff check .`)
- Level 1: Unit tests pass — `SUMMARIZATION_SYSTEM_PROMPT` matches OpenClaw's text; initial path uses `SUMMARIZATION_PROMPT`; update path uses `UPDATE_SUMMARIZATION_PROMPT`; conversation wrapped in `<conversation>` tags; prior summary wrapped in `<previous-summary>` tags
- Level 1: `MERGE_SUMMARIES_INSTRUCTIONS` and `_merge_summaries()` removed (no longer needed)
- Level 2: `CONTEXT_WINDOW_OVERRIDE=5000 bash run_magic_tower.sh 15` — compaction triggers, log output shows structured summary with `## Goal` / `## Progress` / `## Next Steps` sections
