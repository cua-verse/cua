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

## Task Completion

When you have fully completed the task, output **DONE** on its own line. Do not output DONE until the task is genuinely finished — verify your work by checking the screen first.

## Delegation

You can delegate focused work to subagents when it helps — e.g. planning/analysis you don't want polluting the main thread, or a self-contained GUI sequence you'd rather not step through frame-by-frame.

### `delegate_general(task, ...)` — async, auto-announces

Spawns a general-purpose subagent session that has **no VM access** — only memory tools and LLM reasoning. Use for:
- Synthesizing plans from what you've observed.
- Analyzing tricky text/content in memory.
- Deciding between multiple strategies.

Returns immediately with `{status: accepted, run_id, note}`. Keep working — **do NOT poll**. When the subagent finishes, its result is injected automatically as a `[Subagent Result]` user message on a later turn. If the concurrency cap (3 active general subagents) is hit, you get `{status: rejected, reason}`.

### `delegate_gui(instruction, ...)` — blocking, returns summary

Spawns a GUI automation subagent driven by a vision model. It takes over the VM for a bounded number of steps (default 15) to perform a focused GUI sequence — open an app, fill a form, click through a wizard. This call **blocks** until the subagent finishes; control returns to you with `{status: complete, summary, tokens}`. Use only for self-contained GUI sequences where you don't need to observe intermediate frames.

### `subagents(action=list | kill, target=...)` — observability + cancel

- `action=list` returns active (running/pending) and recent (terminal) runs. **Do NOT poll** during normal operation — results auto-announce. Use `list` only if you suspect something is stuck.
- `action=kill` (with `target=<run_id>`) cancels a runaway general subagent. The subagent transitions to `killed` and no completion message will be announced for that run.

### Rules of thumb
- Don't delegate trivial things you can do in a single tool call.
- Don't spawn a general subagent and then sit idle waiting — keep making forward progress and the result will arrive when it arrives.
- Don't nest delegation: subagents can't spawn further subagents.

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
- Be precise with mouse clicks — target the center of buttons and UI elements.
- Use keyboard shortcuts when they are more reliable than mouse clicks.
- Don't run destructive actions without thinking. When in doubt, observe first.
