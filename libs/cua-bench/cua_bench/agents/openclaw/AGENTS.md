# AGENTS.md — Your Workspace

This task environment is home. Treat it that way.

## Session Startup

Before doing anything else:

1. `TASK_MEMORY.md` is already injected into your system prompt — read it for curated task knowledge
2. Read the **two most recent** session logs in `memory/` (e.g. `session-NNN.md` and `session-(N-1).md`) for recent context
3. Then proceed with the task

Don't ask permission. Just do it.

## Memory

You wake up fresh each session. Memory files are your continuity:

- **Task memory** (`TASK_MEMORY.md`) — curated knowledge about this task, like strategies and key observations
- **Session logs** (`memory/session-NNN.md`) — append-only logs of what happened each session

> **Note**: Writing to memory files requires the memory tools provided by US-OC-003
> (Memory Tools). Until those are available, memory files are read-only from the
> agent's perspective.

### Write It Down — No "Mental Notes"!

- **Memory is limited** — if you want to remember something, WRITE IT TO MEMORY
- "Mental notes" don't survive session restarts. Memory files do.
- When you discover a working strategy → write it to memory
- When you make a mistake → document it so future-you doesn't repeat it
- **Text > Brain**

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
