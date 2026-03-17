# OpenClaw Reproduction Plan for CUA

> **For future story plans**: See `docs/plan/US-OC-001-system-prompt-builder.md` for an example of a well-structured **Design Rationale** section. Every story reproducing an OpenClaw component should follow that pattern.

## Context

We're reproducing OpenClaw's agent-side architecture within the CUA benchmark harness. OpenClaw is a large multi-channel AI gateway (~2000+ TS files), but only a subset is relevant to CUA's agent loop. This plan identifies every component, classifies it by relevance, and defines the implementation order.

**Key decisions:**
- All new modules live in the **CUA submodule** under `agents/openclaw/` subfolder
- `openclaw_agent.py` stays at `agents/` level, imports from `agents.openclaw.*`
- Memory: **simple keyword search first**, embedding upgrade as separate story
- Session persistence: **multiple runs per task** with history loaded on each run
- Cross-task knowledge transfer: **future feature**, design format to support it
- **Full 50+ step VM tests** for each component (B1-B5), not just B6

---

## Component Inventory

### Category A: Irrelevant to CUA (Skip)

Platform/infrastructure with no agent-side equivalent:

| Component | Why skip |
|-----------|----------|
| Channel plugins (discord/, telegram/, slack/, signal/, web/, imessage/, line/) | CUA talks to a VM, not chat channels |
| Gateway (gateway/, TypeBox protocol) | WebSocket hub for multi-client |
| ACP (Agent Control Protocol) | External client session binding |
| CLI (cli/, commands/) | OpenClaw's entry point — CUA has batch solver |
| Auto-reply / Command queue | Message routing, debouncing, `/commands` |
| Routing (routing/) | Session key resolution, account lookup |
| Plugins (plugins/) | Plugin discovery/loading |
| Security (security/) | Tool auditing — CUA has own sandboxing |
| Markdown formatting | Channel-specific rendering |
| Streaming / Chunking | Block streaming, preview streaming |
| Typing indicators / Presence | Chat UX |
| OAuth / Auth profiles | Multi-provider auth cycling |
| Model failover | Auth rotation + model fallback chain |
| Send policy | Allow/deny per session |
| Retry policy (outbound) | Per-channel retry |
| Usage tracking | Provider quota dashboards |
| Multi-agent routing | Multiple agents per gateway |
| Browser automation (browser/) | Playwright — CUA has computer tool |
| Media processing (media/) | Audio/video for chat |
| Hooks (hooks/) | Gmail, extensibility |
| Terminal UI / Logging / Infra | CUA has Python equivalents |

### Category B: Needs Reproduction (6 components)

#### B1. System Prompt Builder
- **OpenClaw source**: `agents/system-prompt.ts`, `agents/pi-embedded-helpers/bootstrap.ts`
- **What it does**: Assembles structured prompt from modular sections: identity, tooling guidance, safety, workspace context, time/timezone, memory recall instructions, project context (bootstrap files with truncation)
- **CUA status**: Hardcoded simple string in `openclaw_agent.py`
- **Work**: Build modular prompt builder with sections: identity, tool guidance, memory recall instructions, task context, time. Skip: skills, messaging, reply tags, voice/TTS, reactions, channel-specific.
- **Dependencies**: None (standalone)
- **VM test**: 50+ steps — verify agent follows structured instructions (compare behavior with/without prompt builder)

#### B2. Context Management (Compaction + Pruning)
- **OpenClaw source**: `agents/compaction.ts`, `agents/pi-embedded-runner/compact.ts`, `agents/pi-embedded-runner/extensions.ts`
- **What it does**: Token estimation (chars/4), detect context overflow → multi-stage summarization with identifier preservation, tool result truncation, session pruning (trim old tool results in-memory)
- **CUA status**: `ImageRetentionCallback` (keeps N images) but no text compaction
- **Work**: Token tracking, compaction pipeline (detect overflow → summarize → inject summary), tool result truncation. Implement as CUA Callback using `on_llm_start`.
- **Dependencies**: B4 (needs session history to summarize)
- **VM test**: 50+ steps — long task that would exceed context without compaction; verify agent doesn't crash/degrade

#### B3. Memory System
- **OpenClaw source**: `memory/` (manager.ts, hybrid.ts, embeddings.ts), `agents/tools/memory-tool.ts`, `agents/memory-search.ts`
- **What it does**: Markdown storage (MEMORY.md + daily logs), hybrid search, embedding providers, tools (`memory_search`, `memory_get`), pre-compaction memory flush
- **CUA status**: Legacy `memory/` with simple keyword search. Tools defined but not wired.
- **Work (this story)**: Simple keyword-based search, wire `memory_search` + `memory_get` into agent, daily log persistence, pre-compaction flush hook.
- **Future story**: Upgrade to SQLite + embeddings.
- **Dependencies**: Independent storage layer; tool wiring depends on B5
- **VM test**: 50+ steps — verify agent writes and recalls memory across steps

#### B4. Session Persistence
- **OpenClaw source**: `config/sessions/`, `sessions/`, `auto-reply/reply/session.ts`
- **What it does**: Session state as JSON (ID, model overrides, token metrics), Pi transcripts as JSONL, session loading/restoration, reset policy, override carry-forward
- **CUA status**: No persistence — each run starts fresh
- **Work**:
  - Session state file per task (JSON): task ID, run number, step count, token usage, compaction state
  - Transcript persistence (JSONL): save conversation history per run
  - Multi-run support: load previous run's history on new run, carry forward compaction summaries
  - Design format to support future cross-task knowledge transfer
- **Dependencies**: Independent
- **VM test**: 50+ steps — stop and resume a task across runs; verify history is restored and agent continues coherently

#### B5. Tool System Adaptation
- **OpenClaw source**: `agents/pi-tools.ts`, `agents/pi-tools.policy.ts`
- **What it does**: ~20+ tools with policy layers, schema normalization, before/after hooks
- **CUA status**: Computer + MilestoneTool using BaseTool/@register_tool
- **Work**: Keep: Computer, Milestone. Add: Memory tools (from B3). Optional: planning/notes tool. Add before/after tool call logging hooks via CUA callbacks.
- **Dependencies**: B3 (memory tools)
- **VM test**: 50+ steps — verify all tools are callable and produce correct results

#### B6. Agent Loop Integration (Orchestrator)
- **OpenClaw source**: `agents/pi-embedded-runner/`, `agents/pi-embedded-subscribe.ts`
- **What it does**: Orchestrates model selection, session loading, prompt building, tool registration, streaming, compaction triggers, pre-compaction memory flush
- **CUA status**: `OpenClawAgent.perform_task()` wraps CUA's `ComputerAgent` with basic step tracking
- **Work**: Enhance `perform_task()` to: build system prompt (B1), load session (B4), manage context (B2), register tools (B5), trigger compaction on overflow, flush memory before compaction. Use CUA callbacks as hook points.
- **Dependencies**: All of B1-B5
- **VM test**: 50+ steps — full end-to-end with all components active

### Category C: Directly Use from CUA SDK (No work)

| CUA Feature | OpenClaw Equivalent | Notes |
|-------------|-------------------|-------|
| `ComputerAgent` | Pi agent core | Already using |
| `Computer` tool | exec/browser | Already using |
| `ImageRetentionCallback` | Image context mgmt | Keep N most recent (currently 3) |
| `TrajectorySaverCallback` | Transcript saving | Enable for B4 |
| `BudgetManagerCallback` | Token budget | Enable for B2 |
| `PromptInstructionsCallback` | System prompt injection | Use for B1 |
| `LoggingCallback` | Verbose logging | Available |
| `BaseTool` / `@register_tool` | Tool registry | Already using |
| Callback system (`on_*`) | Pi agent hooks | Use for B2, B4, B6 |

---

## Implementation Order

```
Phase 1 (Independent — can parallelize):
  B1. System Prompt Builder      — standalone, no deps
  B3. Memory System (simple)     — standalone storage layer
  B4. Session Persistence        — standalone state layer

Phase 2 (Depends on Phase 1):
  B2. Context Management         — needs B4 (session history)
  B5. Tool System                — needs B3 (memory tools)

Phase 3 (Integration):
  B6. Agent Loop Integration     — wires B1-B5 together

Future stories:
  - B3+: Memory embedding upgrade (SQLite + vector search)
  - B4+: Cross-task knowledge transfer (index past task transcripts)
```

---

## File Structure

```
submodules/cua/libs/cua-bench/cua_bench/agents/
├── openclaw_agent.py          # Main harness entry point (modify for B6)
└── openclaw/                  # NEW subfolder
    ├── __init__.py            # Package exports
    ├── prompt.py              # B1 — System prompt builder
    ├── context.py             # B2 — Context management callback
    ├── memory.py              # B3 — Memory store + search + tools
    ├── session.py             # B4 — Session persistence
    └── tools.py               # B5 — Tool registration + hooks
```

---

## Cross-Session Knowledge Design (OpenClaw reference)

OpenClaw's memory serves as a cross-session knowledge bridge via 3 layers:

1. **Workspace Memory** (MEMORY.md + daily logs) — shared across all sessions, always indexed
2. **Session transcript indexing** (experimental) — opt-in, indexes past JSONL transcripts into memory search
3. **Inter-session tools** (sessions_history, sessions_send) — explicit, not indexed

**Our mapping:**
- Layer 1 → within-task memory (MEMORY.md + daily logs shared across runs within a task)
- Layer 2 → future cross-task feature (index past task transcripts)
- Layer 3 → not needed (no concurrent sessions in CUA)

The session persistence format (B4) should store transcripts in a way that Layer 2 can index them later without rewriting.

---

## Verification (per component)

Each component story requires:
1. **Unit tests** — core logic (prompt assembly, token estimation, memory search, session save/load)
2. **Integration test** — with CUA's ComputerAgent (mock computer, verify callback pipeline)
3. **VM test** — 50+ steps against a real task, mandatory pass before shipping
