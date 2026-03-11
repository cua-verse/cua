# AGENTS.md — Your Workspace

This task environment is home. Treat it that way.

## Session Startup

Before doing anything else:

1. Run `memory_search` for this task — check for prior observations, strategies, or game state
2. If results found, use `memory_get` to read the relevant files
3. Then proceed with the task

Don't ask permission. Just do it.

## Memory

You wake up fresh each session. Memory files are your continuity:

- **Task memory** (`TASK_MEMORY.md`) — observations and strategies specific to this task
- **Session logs** (`session-NNN.md`) — raw logs from previous runs
- **Daily notes** (`YYYY-MM-DD.md`) — cross-task observations for the day
- **Long-term** (`MEMORY.md`) — curated knowledge that persists across tasks

### Write It Down — No Mental Notes

Memory is limited — if you want to remember something, **write it to memory**.
"Mental notes" don't survive session restarts. Files do.

- Discover a useful strategy → `memory_write` to task memory
- Learn something about the environment → `memory_write` to daily notes
- Make a mistake → document it so future-you doesn't repeat it

## Task Completion

When you have fully completed the task, output **DONE** on its own line. Do not output DONE until the task is genuinely finished — verify your work by checking the screen first.

## Milestones

Use `save_milestone_screenshot` to capture important progress checkpoints. Save a milestone when you:
- Complete a significant sub-goal
- Reach a state that would be hard to reproduce
- Are about to attempt something risky

## General Behavior

- Observe the screen carefully before acting. Read text, check UI state, and plan your next action.
- If you are stuck or an action fails, try an alternative approach rather than repeating the same action.
- Be precise with mouse clicks — target the center of buttons and UI elements.
- Use keyboard shortcuts when they are more reliable than mouse clicks.
- Don't run destructive actions without thinking. When in doubt, observe first.
