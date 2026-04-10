# OpenClaw Investigation: Embedded Runtime vs Utility Helpers

## Purpose

This document summarizes the OpenClaw reference investigation for helper-like tasks that AgentHLE currently implements with direct LiteLLM calls or standalone tools.

The specific question was:
- which OpenClaw flows use the shared embedded agent/runtime stack
- which flows remain lightweight utility helpers
- what that implies for AgentHLE stories around `US-OC-046` and follow-up runtime unification work

## Short Answer

OpenClaw does **not** use one architecture for every helper.

It splits the world into two buckets:

- **Embedded runtime flows**
  Flows that behave like constrained agent turns and benefit from the same model/runtime/session stack as the main agent.

- **Utility helper flows**
  Narrow model or tool calls that return a focused result and do not need full transcript/session behavior.

The practical mapping from the reference is:

- **Memory flush**: embedded runtime
- **Compaction**: embedded/session runtime
- **Vision/image analysis**: utility helper, not embedded runtime

## Findings

### 1. Memory Flush Uses the Embedded Runtime

OpenClaw routes pre-compaction memory flush through `runEmbeddedPiAgent(...)`, not a raw helper completion call.

Reference:
- `../openclaw/src/auto-reply/reply/agent-runner-memory.ts`

Observed behavior:
- memory flush is launched as a constrained embedded run
- it inherits provider/model selection from the same runtime stack
- it reuses the same thinking level machinery and provider wrappers as the normal agent
- it writes through the same session-oriented system rather than bypassing runtime policy

Why OpenClaw does this:
- memory flush is effectively an agentic turn with side effects
- it can invoke memory-write behavior
- it benefits from consistent transcript policy, provider transport handling, and fallback logic

Implication for AgentHLE:
- if we want OpenClaw-style parity, memory flush is the strongest candidate for helper-runtime unification

### 2. Compaction Also Uses the Embedded Session/Runtime Stack

OpenClaw compaction is not implemented as a plain text summarizer helper detached from runtime state. It creates and uses an embedded agent session with `thinkingLevel`, session manager, resource loader, runtime extensions, and transcript sanitization before history limiting/rewrite.

Reference:
- `../openclaw/src/agents/pi-embedded-runner/compact.ts`
- `../openclaw/src/agents/pi-embedded-runner/extensions.ts`

Observed behavior:
- compaction creates an agent session through the embedded runner stack
- runtime extensions and transcript policy participate in the flow
- session history is sanitized and validated before compaction proceeds
- compaction is therefore tightly coupled to runtime/session semantics, not merely “call a summarizer model on some text”

Why OpenClaw does this:
- compaction must preserve runtime-valid transcript structure
- compaction interacts with session state, tool-result pairing, and transcript repair/safeguards
- using the same runtime stack reduces divergence between normal turns and transcript rebuilds

Implication for AgentHLE:
- OpenClaw is a strong argument for runtime unification here too
- but compaction carries more runtime baggage than memory flush, so AgentHLE still needs an explicit architecture decision on whether full parity is worth the cost

### 3. Vision/Image Analysis Is a Utility Helper

OpenClaw’s image tool does **not** use the embedded agent runner. It remains a utility tool path that resolves an image-capable model, builds an image context, and calls the model directly.

Reference:
- `../openclaw/src/agents/tools/image-tool.ts`
- `../openclaw/src/media-understanding/runner.ts`

Observed behavior:
- the image tool resolves an image-capable model configuration
- it builds a focused image prompt/context
- it calls the model directly via the tool/runtime helper layer
- it returns a normal tool result
- it does not appear to expose its own separate embedded-agent thinking-level pipeline

Important nuance:
- OpenClaw often skips extra image-understanding helper work entirely when the active model already supports vision natively
- in that case images are injected into the main model context instead of requiring a separate helper tool call

Why OpenClaw does this:
- image analysis is a narrow operation
- it does not need full agent-session statefulness
- embedded runtime overhead would be high relative to the value

Implication for AgentHLE:
- vision should remain a utility helper
- vision should not be treated as evidence that every helper must migrate into the embedded runtime

## What the Embedded Runtime Buys OpenClaw

For memory flush and compaction, using the embedded runtime gives OpenClaw:

- unified model/provider resolution
- unified thinking-level handling
- unified provider-specific transport wrappers
- unified transcript-policy application
- shared session manager and transcript repair/safeguards
- consistent runtime extensions and state handling

This is the architectural reason OpenClaw does not suffer from the exact split AgentHLE hit, where the main GPT-5.4 runtime supported Responses-style reasoning but helper `acompletion()` paths did not.

## What the Embedded Runtime Costs

The OpenClaw reference also makes clear that the embedded runtime is not free.

Using it brings:
- session manager semantics
- resource loader / extension machinery
- runtime event handling
- transcript/state side-effect concerns
- heavier setup than a simple helper completion call

That is why OpenClaw does not use it for vision/image analysis. The full runtime is appropriate when the helper behaves like an agentic turn, not when the helper is just a focused utility call.

## Design Conclusions for AgentHLE

### Conclusion 1: Memory Flush Should Be the First Unification Candidate

This is the clearest OpenClaw-aligned follow-up.

Reason:
- memory flush already behaves like an agentic turn with side effects
- it is exactly where OpenClaw uses `runEmbeddedPiAgent(...)`
- it gains the most from runtime unification with the least conceptual mismatch

Tracked in:
- `US-OC-048`

### Conclusion 2: Compaction Needs an Explicit Strategy Decision

OpenClaw uses the embedded/session runtime for compaction, but AgentHLE should still make an explicit call on whether to copy that architecture fully or retain a narrower summarizer path with clearly documented constraints.

Reason:
- compaction benefits from runtime parity
- but it also introduces more runtime/state complexity than memory flush
- this is an architecture decision, not an automatic port

Tracked in:
- `US-OC-049`

### Conclusion 3: Vision Should Stay a Utility Helper

The OpenClaw reference does not support “route vision into the embedded runtime” as the default parity direction.

Reason:
- OpenClaw image analysis is a utility path, not an embedded-agent turn
- OpenClaw often skips helper vision when the active model already has native vision
- forcing embedded-runtime parity here would be copying the wrong part of the architecture

Implication:
- AgentHLE should keep `AnalyzeImageTool` as a utility helper
- any future work should focus on supported transport/thinking semantics for that helper, not on migrating it into the embedded runtime

## Story Mapping

This investigation directly informs the current story set:

- `US-OC-046`
  Keep focused on runtime transcript/thinking parity and helper transport correctness.

- `US-OC-048`
  Route memory flush through the shared embedded runtime.

- `US-OC-049`
  Make an explicit compaction architecture decision: embedded runtime vs pure summarizer.

No separate vision-runtime unification story is needed.

## References

- `../openclaw/src/auto-reply/reply/agent-runner-memory.ts`
- `../openclaw/src/agents/pi-embedded-runner/compact.ts`
- `../openclaw/src/agents/pi-embedded-runner/extra-params.ts`
- `../openclaw/src/agents/pi-embedded-runner/run/attempt.ts`
- `../openclaw/src/agents/tools/image-tool.ts`
- `../openclaw/src/media-understanding/runner.ts`
