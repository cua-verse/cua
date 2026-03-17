# OpenClaw Agent Harness — Design Docs Onboarding

This directory contains the design documentation for reproducing OpenClaw's agent-side architecture within the CUA framework. Read this guide to get oriented.

## What is this project?

AgentHLE is a benchmark for evaluating AI agents on computer-use tasks running on remote Windows VMs. The agent harness (code name "OpenClaw reproduction") faithfully ports OpenClaw's agent architecture — memory, context management, session persistence, and compaction — into CUA's `ComputerAgent` framework.

## Reading Order

### 1. Start here — understand the big picture

| File | What you'll learn |
|------|-------------------|
| `development-route-map.md` | Visual dependency graph of all stories, current progress (67%), and what's next |
| `rationale-openclaw-python-port.md` | *Why* we're porting OpenClaw to Python/CUA instead of using it directly |
| `openclaw-source-analysis.md` | Deep analysis of the original OpenClaw TypeScript codebase — the "golden reference" |

### 2. Then — understand the requirements and progress

| File | What you'll learn |
|------|-------------------|
| `prd.json` | All user stories with acceptance criteria, priorities, and pass/fail status |
| `progress.txt` | Codebase patterns (top) + per-story implementation notes and learnings |
| `testing-feedback-loops.md` | The 3-level verification system (unit → VM → multi-session) |

### 3. Finally — dive into specific story plans

Story plans live in `plans/`. Each plan contains context, implementation steps, acceptance criteria, and file references. Read them on-demand based on the area you're working in.

#### By component area:

**Prompt & Memory**
- `plans/US-OC-001-system-prompt-builder.md` — modular prompt assembly
- `plans/US-OC-002-memory-store.md` — persistent memory backend
- `plans/US-OC-003-memory-tools.md` — search/get/write tools for the agent

**Session & Replay**
- `plans/US-OC-004-session-persistence.md` — cross-run state persistence
- `plans/US-OC-004a-session-state-extension.md` — session state schema
- `plans/US-OC-004b-transcript-format-fixes.md` — JSONL transcript format
- `plans/US-OC-022-replay-format-fix.md` — Responses API replay format

**Context Management**
- `plans/US-OC-005-context-overflow.md` — overflow detection callback
- `plans/US-OC-005a-memory-flush-tracking.md` — memory flush wiring
- `plans/US-OC-006-compaction-pipeline.md` — conversation compaction
- `plans/US-OC-013-budget-aware-compaction.md` — token-budget-aware splitting
- `plans/US-OC-018-structured-summarization.md` — structured summary format

**Agent Loop & Integration**
- `plans/US-OC-007-tool-registry-logging.md` — tool registry and logging hooks
- `plans/US-OC-008-agent-loop-integration.md` — wiring everything into the agent
- `plans/US-OC-014-transcript-fidelity.md` — capturing on_llm_start messages
- `plans/US-OC-017-custom-loop-architecture.md` — custom loop replacing stop-compact-resume

**Other**
- `plans/US-OC-024-agents-md-memory-guidance.md` — AGENTS.md enrichment
- `plans/US-OC-011-abort-resume-dropped.md` — dropped story (for context only)
- `plans/openclaw-reproduction-plan.md` — original high-level reproduction plan

## Architecture at a glance

```
openclaw_agent.py ──┬──→ prompt.py       (system prompt assembly)
                    ├──→ memory.py       (persistent memory store)
                    ├──→ tools.py        (memory search/get/write tools)
                    ├──→ session.py      (session state + transcript replay)
                    ├──→ compaction.py   (context compaction pipeline)
                    ├──→ callbacks/      (overflow, flush, budget callbacks)
                    └──→ CUA SDK        (ComputerAgent, Computer tool)
```

## Key concepts

- **Three-phase build**: Phase 1 (independent modules) → Phase 2 (context management) → Phase 3 (agent loop integration). Phases 1–3 are complete; remaining work is cross-cutting.
- **Golden reference**: The original OpenClaw TypeScript source. Every component documents which OpenClaw file(s) it reproduces.
- **Three-level verification**: L1 = unit tests, L2 = 50+ step VM runs, L3 = multi-session continuity tests.
- **CUA submodule**: The agent harness lives inside the CUA repo at `libs/cua-bench/cua_bench/agents/openclaw_agent.py`. OpenClaw modules live in `libs/cua-bench/cua_bench/agents/openclaw/`.
