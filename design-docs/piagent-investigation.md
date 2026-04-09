# PiAgent Investigation Notes

## Scope

This note captures the local investigation into how OpenClaw's Pi Agent works,
where the current CUA/OpenClaw port matches it, where it diverges, and which
stories should own the remaining gaps.

The investigation was driven by four questions:

1. How close is the current harness to OpenClaw's Pi Agent?
2. How does Pi Agent handle reasoning, replay, tool exposure, and runtime control?
3. Does OpenClaw use a single merged Pi Agent loop for main turn + memory flush +
   summarization, or a single orchestrator with multiple helper calls?
4. Which remaining stories should own the identified gaps?


## Executive Summary

The current codebase is a solid reproduction of Pi Agent's core mutable-loop,
compaction, memory, and transcript ideas inside CUA, but it is not yet a close
reproduction of OpenClaw's full Pi runtime control plane.

The most important conclusion from this investigation is:

- OpenClaw does **not** appear to merge the main agent turn, memory flush, and
  compaction summarization into one literal LLM call.
- OpenClaw uses a **single orchestrator/control flow** that invokes distinct
  helper subcalls for:
  - preflight compaction
  - memory flush
  - the main agent turn
- Our code should therefore target a **shared runtime/control-plane adapter**,
  not a monolithic single-call design.


## What Matches Pi Agent Well

### 1. Mutable in-place loop with compaction

The current `OpenClawComputerAgent.run()` keeps a mutable message list and
compacts in place, which is directionally faithful to OpenClaw's
`replaceMessages()` pattern.

Relevant code:

- [agent_loop.py](/media/volume/MOL-System/agenthle-base/submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/agent_loop.py)

### 2. Memory flush before compaction

The local loop runs memory flush pre-API from a single call site before the
main model call, which matches the OpenClaw orchestration shape.

Relevant code:

- [agent_loop.py](/media/volume/MOL-System/agenthle-base/submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/agent_loop.py)
- [memory_flush.py](/media/volume/MOL-System/agenthle-base/submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/memory_flush.py)

### 3. Session/transcript model

The current code already preserves an append-only transcript, compaction
entries, session state, and replay-oriented sanitation. That is strongly aligned
with Pi Agent's session architecture even if the policy surface is still
narrower.

Relevant code:

- [session.py](/media/volume/MOL-System/agenthle-base/submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/session.py)
- [canonical.py](/media/volume/MOL-System/agenthle-base/submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/canonical.py)


## Main Gaps Identified

### 1. Computer tool prompt parity

The runtime exposes the computer tool to the model through tool schemas, but
the system prompt omits it from the tool summary section because prompt
summaries currently filter to `BaseTool` instances only.

Impact:

- The model can use the computer tool, but the prompt does not explicitly
  explain its main desktop interaction capability.

Story owner:

- `US-OC-048`

### 2. Transcript API metadata is wrong

Assistant transcript entries are currently hardcoded as
`api="openai-responses"` even when the active model is Anthropic.

Impact:

- replay/audit metadata is misleading
- provider-aware transcript behavior becomes harder to trust

Story owner:

- `US-OC-049`

### 3. Runtime control plane is still thin

The largest Pi parity gap is not the agent loop itself; it is the missing
runtime control plane around it. OpenClaw Pi does much more centralized work
before and around the main attempt:

- provider/model resolution
- fallback policy
- auth/runtime setup
- transcript policy inputs
- helper transport selection

Our code still spreads those choices across separate local call sites and raw
model-string checks.

Story owner:

- `US-OC-047`

### 4. Replay/transcript sanitation is narrower than OpenClaw

The current replay pipeline already handles thinking blocks, signatures,
provider-aware sanitation, and pairing repair, but it is still narrower than
OpenClaw's broader session-history repair and normalization pipeline.

Story owners:

- `US-OC-032`
- `US-OC-044`
- `US-OC-045`


## Reasoning Handling Findings

OpenClaw Pi preserves reasoning at write time and sanitizes it at replay time
based on provider/runtime policy. The local code broadly matches that design.

What already matches:

- reasoning retained as `thinking` blocks in session-side transcript content
- `thinkingSignature` preserved when available
- Anthropic replay drops thinking blocks
- OpenAI replay keeps valid reasoning and drops orphaned reasoning

What is still weaker than OpenClaw:

- policy resolution still depends too much on raw model-string parsing
- fewer transcript-policy flags are implemented
- replay normalization breadth is smaller
- no Pi-style reasoning streaming/UI control surface

Primary owners:

- `US-OC-047`
- `US-OC-032`
- `US-OC-044`
- `US-OC-045`


## Investigation: "Single PiAgent Loop"

The original question was whether current separate LiteLLM calls for:

- main CUA turn
- summarization
- memory flush

should be merged into a single PiAgent loop.

### What OpenClaw actually does

The OpenClaw runner executes these in one orchestrated flow:

1. preflight compaction
2. memory flush if needed
3. main agent turn

Relevant files:

- [agent-runner.ts](/media/volume/MOL-System/openclaw/src/auto-reply/reply/agent-runner.ts)
- [agent-runner-memory.ts](/media/volume/MOL-System/openclaw/src/auto-reply/reply/agent-runner-memory.ts)

But the helper phases remain distinct calls:

- preflight compaction calls `compactEmbeddedPiSession(...)`
- memory flush calls `runEmbeddedPiAgent(...)`
- summarization remains its own subsystem in `compaction.ts`

Relevant files:

- [compact.ts](/media/volume/MOL-System/openclaw/src/agents/pi-embedded-runner/compact.ts)
- [compaction.ts](/media/volume/MOL-System/openclaw/src/agents/compaction.ts)

### What that means for this repo

The right target is not "one giant LLM call."

The right target is:

- one runtime/controller object per run
- one orchestrated flow
- shared runtime metadata and transport decisions
- separate underlying call methods for:
  - main turn
  - memory flush
  - compaction summarization

That is why `US-OC-047` should own a shared runtime/helper adapter rather than a
monolithic merged-call design.


## Story Mapping

### Highest priority parity work

- `US-OC-048`: expose the computer tool in prompt summaries
- `US-OC-049`: provider-aware transcript API metadata
- `US-OC-047`: shared runtime control plane and helper adapter
- `US-OC-032`: broader replay compatibility and normalization

### Supporting replay/reasoning parity

- `US-OC-044`: OpenAI reasoning/tool-call downgrade rules
- `US-OC-045`: write-time transcript validity guard


## PRD Changes Made During This Investigation

The following PRD changes were made to reflect the findings:

- Revised `US-OC-032` to explicitly own broader replay parity
- Revised `US-OC-047` to own runtime control-plane parity, not just model metadata
- Added `US-OC-048` for the computer-tool prompt gap
- Added `US-OC-049` for provider-aware transcript API metadata
- Tightened `US-OC-047` acceptance criteria so it now explicitly requires:
  - a shared resolved runtime/helper adapter
  - removal of helper-local provider branching
  - one source of truth for helper transport mode, transcript api label,
    context window preference, and transcript-policy inputs
- Updated `US-OC-049` so it now depends on `US-OC-047` rather than allowing a
  parallel local resolver


## Recommended Next Steps

1. Land `US-OC-048` first because it is small and directly fixes a real prompt
   parity gap.
2. Land `US-OC-047` next so there is one runtime object for main turn, memory
   flush, compaction summarization, transcript labeling, and policy inputs.
3. Land `US-OC-049` on top of `US-OC-047` by consuming the runtime object's
   `transcript_api_label`.
4. Continue replay hardening in `US-OC-032`, `US-OC-044`, and `US-OC-045`.
