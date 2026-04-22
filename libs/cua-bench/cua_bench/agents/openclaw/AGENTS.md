# AGENTS.md — Your Workspace

This task environment is home. Treat it that way.

## Session Startup

Before doing anything else:

1. Read the **two most recent** session logs in `memory/` (e.g. `session-NNN.md` and `session-(N-1).md`) for recent context
2. Then proceed with the task

Don't ask permission. Just do it.

## Memory

You wake up fresh each session. Memory files are your continuity.

### Two Memory Layers

- **Session logs** (`memory/session-NNN.md`) — raw logs of what happened this session
  - Append-only. Write observations, actions taken, errors encountered.
  - Think of these as your scratchpad — capture everything, filter nothing.
  - Use `memory_write` with `target='session'`

- **Task memory** (`TASK_MEMORY.md`) — curated knowledge about this task
  - Your distilled wisdom. Strategies that work, patterns discovered, dead ends to avoid.
  - Overwrites the whole file — always include everything worth keeping.
  - Use `memory_write` with `target='task_memory'`

Use the memory tools to interact with these files:
- `memory_search` — keyword search across TASK_MEMORY.md and session logs
- `memory_get` — read a specific file or line range
- `memory_write` — append to session log or overwrite TASK_MEMORY.md

### When to Write What

| What happened | Write to | Example |
|---|---|---|
| Observed something new | session log | "The settings menu has an Export option under File" |
| Tried an action, saw result | session log | "Clicking 'Submit' opens a confirmation dialog" |
| Discovered a working strategy | task_memory | "Always save the document before switching tabs" |
| Made a mistake worth avoiding | session log + task_memory | Log the error, update strategy |
| Reached a milestone | session log | "Completed form setup, all 5 fields configured" |
| Synthesized lessons from multiple sessions | task_memory | Consolidate patterns into durable guidance |

### Write It Down — No "Mental Notes"!

- "Mental notes" don't survive session restarts. Memory files do.
- When you discover a working strategy → write it to task_memory
- When you observe application state → write it to session log
- When you make a mistake → document it so future-you doesn't repeat it

### Memory Consolidation

Before ending a session or when the context is getting long:
- Review what you've learned this session
- Update TASK_MEMORY.md with any durable insights worth keeping across sessions
- Think: "If future-me woke up with only TASK_MEMORY.md, would they have what they need?"

## Staying Alive & Task Completion

**Critical**: The session ends the moment you respond with text and no tool call. There is no "idle" state — if you output text without calling a tool, your session terminates immediately and cannot resume. This applies even if you are waiting for a subagent to finish.

- **To keep working**: always include at least one tool call in your response. When idle, use `computer(action="wait", ms=5000)`.
- **To finish**: output **DONE** on its own line with no tool calls. Do not output DONE until the task is genuinely finished — verify your work by checking the screen first.

## Milestones

Use `save_milestone_screenshot` to capture important progress checkpoints. Save a milestone when you:
- Complete a significant sub-goal
- Reach a state that would be hard to reproduce
- Are about to attempt something risky

### Verifying Milestones

After saving a milestone, verify it with `analyze_image` — provide a prompt describing what to check:

1. `save_milestone_screenshot(path="...", description="...")`
2. `analyze_image(image="<same path>", prompt="<what you need to verify>")`
3. If the result doesn't match your expectation, re-navigate and re-save

You can also analyze local screenshots (from `[Screenshot saved to: ...]` messages) or compare multiple images using the `images` parameter.

## General Behavior

- Observe the screen carefully before acting. Read text, check UI state, and plan your next action.
- If you are stuck or an action fails, try an alternative approach rather than repeating the same action.
- Don't run destructive actions without thinking. When in doubt, observe first.
