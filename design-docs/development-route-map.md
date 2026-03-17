# AgentHLE — OpenClaw Reproduction Development Map

> Auto-generated 2026-03-17. 22/33 stories done (67%).

```
═══ PHASE 1: Independent Modules ════════════════════════════════════

  ✓ US-OC-001 (P1)                ✓ US-OC-002 (P2)         ✓ US-OC-004 (P4)
  System Prompt Builder           Memory Store              Session Persistence
  [prompt.py]                     [memory.py]               [session.py]
       │                               │                         │
       │                               ▼                         ├──→ ✓ US-OC-004a (P4.1)
       │                         ✓ US-OC-003 (P3)               │    Session State Schema
       │                         Memory Tools                    │
       │                         [tools: search/get/write]       ├──→ ✓ US-OC-004b (P4.2)
       │                               │                         │    Transcript JSONL Fixes
       │                               │                         │
       │                               │                         ▼
       │                               │                   ✓ US-OC-012 (P8.1)
       │                               │                   Transcript Replay
       │                               │                         │
       │                               │                         ▼
       │                               │                   ✓ US-OC-022 (P8.2)
       │                               │                   Replay → Responses API
       │                               │
       ▼                               ▼
═══ PHASE 2: Context Management + Tool Wiring ═══════════════════════

  ✓ US-OC-005 (P5)              ✓ US-OC-007 (P7)
  Context Overflow Detection     Tool Registry & Logging
       │                               │
       ├──→ ✓ US-OC-005a (P5.1)        │
       │    Wire Memory Flush           │
       │         │                      │
       │         ▼                      │
       │    ✓ US-OC-025 (P5.2) ◄───────┘
       │    Fix Memory Flush Timing
       │                                │
       ▼                                │
  ✓ US-OC-006 (P6)                     │
  Compaction Pipeline                   │
       │                                │
       ├──→ ✓ US-OC-013 (P12)          │
       │    Budget-Aware Split          │
       │         │                      │
       │         ▼                      │
       │    ✓ US-OC-018 (P12.1)        │
       │    Structured Summarization    │
       │         │                      │
       │         ▼                      │
       │    ✓ US-OC-015 (P13.1)        │
       │    Summarization Timeout       │
       │         │                      │
       │         ▼                      │
       │    ✗ US-OC-016 (P14)          │
       │    Multi-Stage Summarization   │
       │                                │
       ▼                                ▼
═══ PHASE 3: Agent Loop Integration ═════════════════════════════════

              ✓ US-OC-008 (P8)
              Agent Loop Integration
              [openclaw_agent.py — wires everything together]
                     │
                     ├──────────────────────────────┐
                     │                              │
                     ▼                              ▼
         ✓ US-OC-014 (P13)              ✓ US-OC-021 (P18)
         Transcript Fidelity            CLI: --summary-model
         (on_llm_start capture)
                     │
                     ▼
         ✓ US-OC-017 (P15)              ✓ US-OC-028 (P24)
         Custom Loop Architecture        Agent Loop Refactor
                                         (perform_task boundary)


═══ CROSS-CUTTING / ENHANCEMENTS ════════════════════════════════════

  ┌─────────────────────────┐   ┌─────────────────────────┐
  │  Thinking Mode          │   │  Audits & Reviews       │
  │                         │   │                         │
  │  ✗ US-OC-019 (P25)     │   │  ✗ US-OC-009 (P27)     │
  │  Config + CLI + Loop    │   │  CUA SDK Audit          │
  │         │               │   │                         │
  │         ▼               │   │  ✗ US-OC-010 (P28)     │
  │  ✗ US-OC-020 (P26)     │   │  Skipped Component      │
  │  Wire into Flush &      │   │  Review                 │
  │  Compaction             │   │                         │
  └─────────────────────────┘   └─────────────────────────┘

  ┌─────────────────────────┐   ┌─────────────────────────┐
  │  Content & Tools        │   │  Compatibility          │
  │                         │   │                         │
  │  ✗ US-OC-024 (P21)     │   │  ✗ US-OC-023 (P22)     │
  │  AGENTS.md Enrichment   │   │  Opus 4.6 Tool Compat   │
  │                         │   │                         │
  │  ✗ US-OC-029 (P29)     │   │  ✗ US-OC-032 (P32)     │
  │  Subagent Delegation    │   │  Cross-Model Message    │
  │                         │   │  Format Compat          │
  │  ✗ US-OC-030 (P30)     │   │                         │
  │  Visual Analysis Tool   │   └─────────────────────────┘
  │                         │
  │  ✗ US-OC-031 (P31)     │
  │  Tool Audit: Migratable │
  └─────────────────────────┘
```

## Component Map

```
openclaw_agent.py ──┬──→ prompt.py (System Prompt)
                    ├──→ memory.py (Memory Store)
                    ├──→ tools.py  (Memory Tools)
                    ├──→ session.py (Session Persistence + Replay)
                    ├──→ compaction.py (Context Compaction)
                    ├──→ callbacks/ (Overflow, Flush, Budget)
                    └──→ CUA SDK (ComputerAgent, Computer tool)
```

## Progress

| Status  | Count | Bar |
|---------|-------|-----|
| Done    | 22    | ██████████████████████░░░░░░░░░░░ 67% |
| Pending | 11    | ███████████░░░░░░░░░░░░░░░░░░░░░ 33% |

## Priority Queue (Next Up)

| Priority | ID | Title |
|----------|----|-------|
| 21 | US-OC-024 | AGENTS.md Memory Guidance Enrichment |
| 22 | US-OC-023 | CUA Anthropic Loop: Opus 4.6 Tool Version Compat |
| 23 | US-OC-016 | Multi-Stage Summarization for Scale |
| 25 | US-OC-019 | Thinking Mode: Config, CLI, Main Agent Loop |
| 26 | US-OC-020 | Thinking Mode: Wire into Memory Flush & Compaction |
| 27 | US-OC-009 | Post-Eval: CUA SDK Component Audit |
| 28 | US-OC-010 | Post-Eval: Skipped Component Review |
| 29 | US-OC-029 | Tool: Subagent Delegation |
| 30 | US-OC-030 | Tool: Visual Analysis (Figure/Image Reading) |
| 31 | US-OC-031 | Tool Audit: Identify Migratable OpenClaw Tools |
| 32 | US-OC-032 | Cross-Model Message Format Compatibility |

## Key Takeaways

- **Phase 1** (independent modules) is fully complete — prompt, memory, session, replay all done
- **Phase 2** (context management) is nearly complete — only US-OC-016 (multi-stage summarization) remains
- **Phase 3** (integration) is complete — agent loop, transcript fidelity, custom loop architecture, and the perform_task refactor are all shipped
- **New stories** added since last update: US-OC-028 (agent loop refactor), US-OC-029–032 (tools, audits, cross-model compat)
- **Remaining work** is cross-cutting: thinking mode, tool additions (subagent, visual analysis), audits, and compatibility fixes
