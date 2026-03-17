# Rationale: Why Port OpenClaw to Python for CUA?

> Summary of design discussion, 2026-03-16.

## The Problem

CUA provides a VM-based computer-use agent framework with a simple loop (screenshot → action → repeat). This works for short tasks but breaks down on long, multi-step benchmark tasks (50–500+ steps):

- Context window fills up with screenshots after ~20–30 turns
- The model loses track of earlier actions
- Multi-session tasks have zero continuity between runs
- No mechanism to summarize, persist, or replay prior work

## What OpenClaw Provides

OpenClaw is a production TypeScript personal AI assistant that already solved these problems:

- **Context overflow detection** — knowing when you're about to hit the window limit
- **Compaction** — summarizing old turns to free space without losing critical info
- **Memory** — persisting knowledge across sessions
- **Session replay** — reconstructing prior run context for multi-run tasks
- **Budget management** — splitting token budgets between main loop and auxiliary calls

These are hard, interconnected problems with subtle edge cases (when to flush memory, what to keep vs. summarize, how to replay tool calls faithfully). OpenClaw iterated on them in production.

## Why Not Use OpenClaw Directly?

### It's a product, not a library

OpenClaw is a deployed application with its own Node.js server, SQLite database, Slack integrations, and user system. There's no `pip install openclaw` or importable API. To "use" it, you'd need to run its entire stack.

### It doesn't speak CUA

OpenClaw has its own tool system (file editing, shell commands, web browsing, messaging). CUA has a completely different tool surface: `Computer` (mouse/keyboard/screenshot on a remote VM) and `MilestoneTool`. OpenClaw has no concept of remote desktop sessions, screenshot-based interaction, benchmark scoring, or CUA's callback hooks (`on_llm_start`, `on_tool_end`).

### The benchmark requires CUA's pipeline

AgentHLE tasks are CUA task modules. The batch solver manages VM connections, runs agents, and collects scores. The agent **must** be a CUA `BaseAgent` subclass in Python. OpenClaw can't participate in this pipeline without being fundamentally restructured.

### The context logic is tangled into the application

If OpenClaw's context management were a clean, importable module, you could extract it. But compaction assumes OpenClaw's message format, memory assumes its workspace structure, and session replay assumes its conversation model. Extracting these as reusable modules **is** the porting work.

## Why Not Bridge TypeScript and Python?

Several cross-language integration approaches were considered:

| Approach | How | Why it doesn't work |
|----------|-----|---------------------|
| **Subprocess / IPC** | Run OpenClaw as a child process, communicate via stdin/stdout | Every CUA callback would cross the process boundary. At 50+ steps with screenshots, you're serializing megabytes per turn. Latency adds up. |
| **HTTP microservice** | Wrap OpenClaw as a REST API | Same serialization overhead, plus two services to run and maintain on every benchmark machine. |
| **WebAssembly** | Compile TypeScript → Wasm, call from Python | OpenClaw uses Node-specific APIs (fs, SQLite, async I/O). These don't compile to Wasm. Massive effort, uncertain results. |
| **Shared data layer** | Both runtimes read/write the same files | Race conditions, no real-time integration during turns when compaction decisions must happen *within* a callback. |

### The core issue: coupling granularity

OpenClaw's context management isn't a single coarse call like "compact this." It's deeply woven into the per-turn loop:

1. `on_llm_start` → check overflow → decide to compact
2. Compact → summarize chunks → rebuild message array
3. Check memory flush conditions → maybe flush
4. All of this happens **between turns**, touching CUA callback state

Bridging this requires crossing the language boundary **multiple times per turn**, passing the full message array each time. The bridge code would be more complex than the port itself.

## Why Python Port Is the Right Choice

- **Python is not a preference — it's where CUA lives.** The agent harness must be a Python class inside CUA's framework.
- **The port is scoped.** Only ~20% of OpenClaw matters (context management, compaction, memory, replay). The other 80% (Slack, voice, messaging, user management) is irrelevant.
- **Algorithms translate cleanly.** Compaction splitting, keyword search, overflow detection — logic is logic. Python's `async/await` maps directly to TypeScript's.
- **Zero runtime overhead.** No serialization, no IPC, no second runtime to manage.
- **Simpler to debug.** One language, one process, one stack trace.

## What Doesn't Translate Cleanly

- TypeScript's type system (interfaces, unions, generics) → Python uses dataclasses + type hints, less strict
- Node ecosystem (SQLite + embeddings for memory) → simplified to markdown files + keyword matching (sufficient for benchmarks)
- CUA's callback system imposes its own structure that doesn't exist in OpenClaw → OpenClaw logic reshaped to fit CUA's hooks

## Bottom Line

OpenClaw is a **source of proven designs**, not a reusable dependency. This project extracts the architecture patterns that matter and adapts them to a completely different runtime. A clean Python port is less work, simpler to operate, and more reliable than any cross-language bridge.
