# US-OC-019 + US-OC-020: OpenClaw Thinking System

## Context

OpenClaw has a sophisticated thinking/reasoning control system with 7 levels (off/minimal/low/medium/high/xhigh/adaptive) that map to provider-specific API parameters. We want to reproduce this for the CUA agent harness so operators can control reasoning depth via CLI per run. We are NOT implementing Anthropic extended thinking (budget_tokens) as the primary interface — instead, thinking levels map to provider-specific params.

## OpenClaw Design Rationale

**What OpenClaw Does** (`openclaw/src/auto-reply/thinking.ts`, `openclaw/src/agents/model-selection.ts`, `openclaw/src/agents/pi-embedded-runner/thinking.ts`):
- `ThinkLevel`: "off" | "minimal" | "low" | "medium" | "high" | "xhigh" | "adaptive"
- Resolution hierarchy: global default → per-model params → per-session → per-turn directives
- Provider-specific mapping in `extra-params.ts`:
  - Anthropic: beta headers (`interleaved-thinking-2025-05-14`) + thinking param
  - OpenRouter: `reasoning.effort` (none/minimal/low/medium/high/xhigh)
  - Gemini: `thinkingConfig.thinkingLevel` (MINIMAL/LOW/MEDIUM/HIGH)
  - Moonshot: `thinking.type` (enabled/disabled)
- `resolveThinkingDefault()`: Claude 4.6 → "adaptive", reasoning models → "low", others → "off"
- Thinking blocks are **kept in context by default** — compaction sees them. `dropThinkingBlocks()` is only applied conditionally for specific providers (GitHub Copilot Claude) that reject persisted thinking blocks.

**What We Keep and Why**:
- `ThinkLevel` enum with same 7 levels — maintains compatibility with OpenClaw's vocabulary
- CLI per-run thinking level — operators set thinking level per benchmark run
- `resolveThinkingDefault()` — auto-sets sensible defaults per model (Claude 4.6 → adaptive, reasoning models → low)
- Per-call-site config (main loop vs flush vs compaction) — summarization tasks benefit less from deep thinking
- Provider-specific parameter mapping — different providers need different API formats
- Keep thinking blocks in context — matches OpenClaw default; compaction sees them

**What We Drop and Why**:
- Per-session / per-turn overrides — CUA benchmark runs are single-shot, no interactive user
- Full transcript-policy / sanitize pipeline — deferred to US-OC-038/039/041, where message replay and provider-specific sanitization are designed centrally
- Provider-specific thinking sanitization (`dropThinkingBlocks`, signature repair, downgrade passes) — deferred to US-OC-041; US-OC-020 only preserves thinking blocks, it does not make them replay-safe across providers/models

**Future Work** (note in docs, don't implement):
- `VerboseLevel`, `ReasoningLevel`, `ElevatedLevel` — additional control dimensions from OpenClaw. VerboseLevel controls output detail, ReasoningLevel controls chain-of-thought visibility, ElevatedLevel controls permission escalation. Not needed for headless CUA benchmark runs but may be useful for interactive agent modes.
- `dropThinkingBlocks()` — conditional per-provider stripping of thinking blocks from history (OpenClaw: `pi-embedded-runner/thinking.ts:25-53`). This belongs in the future TranscriptPolicy pipeline:
  - US-OC-038 defines the canonical internal message model
  - US-OC-039 defines the centralized sanitize/adapter pipeline
  - US-OC-041 adds provider-specific thinking sanitization passes
- `<think>` tag extraction — some models (DeepSeek R1, older reasoning models) emit reasoning inside `<think>...</think>` XML tags in plain text rather than structured thinking blocks. OpenClaw has `extractThinkingFromTaggedText()`, `splitThinkingTaggedText()`, and `promoteThinkingTagsToBlocks()` in `pi-embedded-utils.ts` to parse and promote these to structured blocks. Implement when adding non-structured reasoning models (DeepSeek R1, QwQ, etc.) — without this, thinking text stays in visible content and pollutes compaction summaries.
- Per-turn thinking level overrides — agent self-adjusting thinking depth based on task difficulty.

## Updated Investigation (2026-03-23)

### 1. Sequencing decision

Implement US-OC-020 before US-OC-038/039/041, but keep the contract narrow.

Reasoning:
- US-OC-020 is additive and local: it wires per-call-site thinking params and stops dropping thinking blocks at the transcript boundary.
- US-OC-038/039/041 are architectural: they define the canonical message model and replay sanitization rules.
- If we design sanitization first while the transcript is still lossy, we risk building adapters around incomplete data.

So the order should be:
1. US-OC-020 — preserve and plumb thinking correctly
2. US-OC-038 — canonical internal message model
3. US-OC-039 — centralized sanitize/adapter pipeline
4. US-OC-041 — provider-specific thinking sanitization passes

### 2. Session message model vs replay model

Current AgentHLE does not have one canonical internal message format. It has multiple related representations:
- CUA output items from the live loop
- Session transcript message content blocks
- Compaction input messages
- Provider-specific API payloads

That matters for thinking support because a thinking block can be:
- present in CUA output
- dropped during transcript grouping
- serialized in a shape that is not safe to replay later

For US-OC-020, the transcript should become a high-fidelity stored history: if a thinking block exists in the step output, we should persist it. But we should not overclaim the transcript as the final canonical replay model. US-OC-038/039 still need to define the canonical item shape and the provider adapters.

### 3. Multi-provider risk

There are two separate questions:
1. Can we store thinking blocks in session history?
2. Can we replay those thinking blocks safely into later model calls?

US-OC-020 answers only the first question. The second is deferred to the sanitize-policy work.

This distinction matters because different providers have different replay constraints:
- some require provider-native reasoning item shapes
- some validate reasoning signatures or IDs on replay
- some reject persisted thinking blocks entirely unless transformed or downgraded

So the explicit US-OC-020 contract should be:
- preserve thinking blocks in transcript/history
- pass thinking params to the relevant LLM call sites
- count thinking content in token estimation/compaction
- do not promise provider-safe replay until US-OC-041

---

## US-OC-019: ThinkingConfig, CLI, and Main Agent Loop

### 1. New file: `agents/openclaw/thinking.py`

```python
from dataclasses import dataclass
from enum import Enum

class ThinkLevel(str, Enum):
    OFF = "off"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    ADAPTIVE = "adaptive"

@dataclass
class ThinkingConfig:
    level: ThinkLevel = ThinkLevel.OFF
    flush_level: ThinkLevel = ThinkLevel.OFF      # for US-OC-020
    compaction_level: ThinkLevel = ThinkLevel.OFF  # for US-OC-020

    def to_api_params(self, model: str) -> dict:
        """Return provider-specific kwargs for ComputerAgent / litellm."""
        return resolve_thinking_params(self.level, model)

    def flush_params(self, model: str) -> dict:
        return resolve_thinking_params(self.flush_level, model)

    def compaction_params(self, model: str) -> dict:
        return resolve_thinking_params(self.compaction_level, model)


def resolve_thinking_default(model: str) -> ThinkLevel:
    """Auto-detect default thinking level based on model capabilities.

    Based on OpenClaw's resolveThinkingDefault() in model-selection.ts.
    - Claude 4.6 (Opus/Sonnet) → adaptive
    - Reasoning models (DeepSeek R1, etc.) → low
    - Others → off
    """
    ...


def resolve_thinking_params(level: ThinkLevel, model: str) -> dict:
    """Map ThinkLevel to provider-specific API params.

    Based on OpenClaw's extra-params.ts provider mappings.
    """
    if level == ThinkLevel.OFF:
        return {}
    # Anthropic → thinking param with budget
    # OpenAI → reasoning.effort
    # Gemini → thinking_level kwarg
    # Fallback → reasoning_effort
    ...
```

Provider-specific mappings:
- **Anthropic**: `{"thinking": {"type": "enabled", "budget_tokens": N}}` where N varies by level (minimal=2k, low=5k, medium=10k, high=16k, xhigh=25k, adaptive=10k)
- **OpenAI**: `{"reasoning": {"effort": "low"|"medium"|"high", "summary": "concise"}}`
- **Gemini**: `{"thinking_level": "MINIMAL"|"LOW"|"MEDIUM"|"HIGH"}` (CUA gemini loop handles this)
- **Fallback**: `{"reasoning_effort": level.value}`

### 2. CLI: `solver.py:parse_args`

Add three flags:
- `--thinking-level`
- `--flush-thinking-level`
- `--compaction-thinking-level`

Thread through `initialize_agent()` into:
- `agent_kwargs["thinking_level"]`
- `agent_kwargs["flush_thinking_level"]`
- `agent_kwargs["compaction_thinking_level"]`

### 3. `OpenClawAgent.__init__`

Resolve the three levels as follows:
- `thinking_level`: explicit CLI value or `resolve_thinking_default(self.model)`
- `flush_thinking_level`: explicit CLI value or inherit `thinking_level`
- `compaction_thinking_level`: explicit CLI value or inherit `thinking_level`

This gives the operator a simple default mental model: if they turn thinking on, all call sites use that level unless explicitly overridden.

```python
main_level = ThinkLevel(kwargs["thinking_level"]) if kwargs.get("thinking_level") else resolve_thinking_default(self.model)
flush_level = ThinkLevel(kwargs["flush_thinking_level"]) if kwargs.get("flush_thinking_level") else main_level
compaction_level = ThinkLevel(kwargs["compaction_thinking_level"]) if kwargs.get("compaction_thinking_level") else main_level
self.thinking_config = ThinkingConfig(
    level=main_level,
    flush_level=flush_level,
    compaction_level=compaction_level,
)
```

### 4. `OpenClawAgent.perform_task` (lines 179-192)

Pass thinking params to `OpenClawComputerAgent`:
```python
agent = OpenClawComputerAgent(
    model=self.model,
    ...
    thinking_config=self.thinking_config,
    **self.thinking_config.to_api_params(self.model),  # → ComputerAgent additional_generation_kwargs
)
```

Print thinking config at init:
```python
if self.thinking_config.level != ThinkLevel.OFF:
    print("  Thinking level:", self.thinking_config.level.value)
```

### 5. Shell entry points

Update task runner scripts to optionally accept and forward:
- main thinking level
- flush thinking level
- compaction thinking level

The shell scripts do not need their own defaulting logic beyond argument pass-through; inheritance should live in Python so all entry points behave the same.

### 6. Export from `__init__.py`

Add `ThinkingConfig`, `ThinkLevel`, `resolve_thinking_default` to `agents/openclaw/__init__.py`.

### Files modified (US-OC-019):
- **NEW**: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/thinking.py`
- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/__init__.py`
- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw_agent.py`
- `submodules/cua/libs/cua-bench/cua_bench/batch/solver.py`
- `run_magic_tower.sh`

---

## US-OC-020: Wire into Memory Flush, Compaction, and Transcript

### 1. `memory_flush.py:run_memory_flush`

Add `thinking_params: dict | None = None` parameter. Spread `**(thinking_params or {})` into `litellm.acompletion()` at line 97.

### 2. `context.py` summarization functions

Add `thinking_params: dict | None = None` parameter to:
- `summarize_chunk`
- `summarize_chunks_iterative`
- `summarize_with_fallback`
- `compact_messages`

Thread the value through to each `litellm.acompletion()` summarization call.

### 3. `agent_loop.py:OpenClawComputerAgent`

- Keep `thinking_config` on the agent loop object
- In `_maybe_flush_memory()`: pass `self.thinking_config.flush_params(self.summary_model)` to `run_memory_flush`
- In `_compact_in_place()`: pass `self.thinking_config.compaction_params(self.summary_model)` to `compact_messages`

### 4. `transcript.py:group_step_output`

Capture thinking blocks from CUA output items into assistant content, preserving at least:

```python
{"type": "thinking", "thinking": "..."}
```

If the provider output includes additional replay-relevant fields such as a thinking signature or ID, preserve them in the transcript too instead of discarding them. US-OC-020 should bias toward loss-minimizing storage even though later sanitize passes may rewrite or drop these fields.

### 5. `context.py` token estimation contract

Make thinking support explicit in the estimator and tests:
- token estimation must include thinking block characters
- this should be tested directly, not only inferred from generic JSON serialization

Even if the current estimator already counts thinking chars indirectly because it serializes the full message dict, US-OC-020 should turn that into a documented and tested contract.

### Files modified (US-OC-020):
- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/memory_flush.py`
- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/context.py`
- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/agent_loop.py`
- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/transcript.py`
- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw_agent.py`
- `submodules/cua/libs/cua-bench/cua_bench/batch/solver.py`
- shell task runner(s)

---

## Verification

1. **Lint**: `uv run ruff check submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/`
2. **Unit tests**:
   - ThinkLevel enum values match OpenClaw's 7 levels
   - `resolve_thinking_default()`: Claude 4.6 → adaptive, reasoning model → low, unknown → off
   - `resolve_thinking_params()`: correct provider-specific output per model string
   - `ThinkingConfig` inheritance: flush/compaction default to main thinking level when unspecified
   - explicit overrides work: main/high + flush/off + compaction/low produce distinct params
   - transcript grouping preserves thinking blocks
   - token estimation explicitly counts thinking block characters
   - memory flush and compaction helpers pass through the supplied thinking params
3. **Level 2**: run with main thinking only — flush and compaction inherit the same level by default
4. **Level 2**: run with explicit overrides — flush/compaction use different params from the main loop

## Non-goals for US-OC-020

- Do not implement provider-safe reasoning replay
- Do not add TranscriptPolicy or sanitize-thinking passes here
- Do not redesign the canonical internal message model here

Those belong to:
- `docs/plan/US-OC-038-040-canonical-format.md`
- future US-OC-041 implementation
