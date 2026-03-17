# US-OC-001: System Prompt Builder — Design Rationale

> **Example for future story plans**: This document demonstrates the required **Design Rationale** structure for all OpenClaw reproduction stories. See the sections below (What OpenClaw Does → What We Kept and Why → What We Dropped and Why → Key Differences) as your template.

## What OpenClaw Does

OpenClaw's `system-prompt.ts` (`buildAgentSystemPrompt`) assembles a custom system prompt for every agent run with 15+ sections: Tooling, Safety, Skills, Self-Update, Workspace, Documentation, Workspace Files (bootstrap injection), Sandbox, Current Date & Time, Reply Tags, Heartbeats, Runtime, Reasoning, and more.

It supports three prompt modes:
- **full** (default): all sections included
- **minimal**: for sub-agents — omits Skills, Memory Recall, Self-Update, Model Aliases, User Identity, Reply Tags, Messaging, Silent Replies, Heartbeats
- **none**: only the base identity line

Bootstrap injection (`contextFiles` pattern) appends workspace files (AGENTS.md, SOUL.md, TOOLS.md, USER.md, MEMORY.md, etc.) under a **Project Context** heading, with per-file and total size caps.

## What We Kept and Why

| Section | Why |
|---------|-----|
| **Identity** | Agent needs a role definition. Adapted from OpenClaw's identity line to describe CUA benchmark context. |
| **Tools** | CUA tools differ from OpenClaw's (Computer, MilestoneTool vs file ops, shell, browser). Listing available tools with descriptions is essential for any agent. |
| **Memory Recall** | Same tool names (`memory_search`, `memory_get`), same search-first directive pattern. Conditionally included only when memory tools are registered. **Note**: OpenClaw has only read-only memory tools — agents write to memory files using generic `write`/`edit` file tools, not a dedicated `memory_write`. Our tool set may differ; see US-OC-003 for the final decision on whether to add a `memory_write` tool or reuse a generic file write tool. |
| **Project Context** | Bootstrap injection is the right vehicle for persistent guidance. AGENTS.md replaces OpenClaw's AGENTS.md (agent behavior rules). TASK_MEMORY.md injected when prior knowledge exists. Task description is NOT injected here — it's passed via `agent.run(instruction)` to avoid duplication. |

## What We Dropped and Why

| Section | Rationale |
|---------|-----------|
| Skills | No skill system in CUA — agent has fixed tools |
| Messaging | No chat channels — agent communicates via actions only |
| Reply Tags | No provider-specific reply formatting needed |
| Voice/TTS | No voice interface |
| Reactions | No emoji reaction system |
| Heartbeats | CUA agent loop handles keep-alive via step iteration |
| Sandbox | CUA runs on a remote VM — sandboxed by design |
| Documentation | No local docs to reference during task execution |
| Self-Update | No update mechanism for the agent |
| Model Aliases | Single model, no aliasing needed |
| User Identity | No user profile — tasks define context |
| Silent Replies | No silent reply mechanism in CUA |
| Safety | CUA is sandboxed by design (remote VM, no persistent effects) |
| Runtime | Not useful for task completion — agent doesn't need to know its own model/host |
| Current Time | Agent can read the VM clock in screenshots. Including time in the prompt would break cache stability for no benefit. |
| Reasoning | No reasoning toggle in CUA |
| SOUL.md / TOOLS.md / IDENTITY.md | OpenClaw-specific persona/tool docs — replaced by AGENTS.md |
| HEARTBEAT.md / BOOTSTRAP.md | No heartbeat system; no workspace lifecycle |

## Key Differences from OpenClaw

1. **AGENTS.md** — Our AGENTS.md focuses on CUA benchmark behavior (DONE signal, milestones, observation strategy). OpenClaw's AGENTS.md covers workspace-specific agent configuration (session startup ritual, persona files, group chat behavior, heartbeats).
2. **No task.md injection** — Task description is passed via `agent.run(instruction)`, not injected into the system prompt. OpenClaw injects USER.md (user identity/preferences) as a context file, but in CUA the task description would be duplicated since ComputerAgent already sends it as a user message. TASK_MEMORY.md is injected when prior knowledge exists.
3. **No Bootstrap.md** — OpenClaw uses Bootstrap.md for first-run workspace setup. CUA tasks are stateless (no workspace lifecycle).
4. **No prompt modes** — We build one prompt. Sub-agent support can be added later if needed.
5. **Conditional Memory section** — Only included when memory tools are registered, unlike OpenClaw where memory is always available.
6. **No `memory_write` tool** — OpenClaw has only `memory_search` and `memory_get` as dedicated memory tools. Agents write to memory files using generic `write`/`edit` file tools. Additionally, OpenClaw has a pre-compaction "memory flush" system that prompts the agent to persist durable memories before context compaction. CUA doesn't have generic file write tools, so US-OC-003 must decide how agents write to memory (dedicated `memory_write` tool vs reusing another mechanism).

## How Tools Are Specified

In the real implementation (US-OC-008), `tool_summaries` will be derived from the registered tool list at runtime. Each CUA `BaseTool` subclass has a `name` and `description` attribute set by `@register_tool`. The integration code in `perform_task()` will build the dict from the actual tool instances:

```python
tool_summaries = {tool.name: tool.description for tool in tools}
```

This means the Tools section automatically reflects whatever tools are registered for that run — no hardcoding needed.

## Future Additions

- **MEMORY.md injection**: Deferred to US-OC-002. In OpenClaw, MEMORY.md is injected via bootstrap (daily logs are tool-accessed only). We need to decide whether to inject it here or access exclusively via memory tools.
- **Prompt modes**: If sub-agents are added, a minimal mode can strip Memory Recall and Project Context.
