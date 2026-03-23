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
- `dropThinkingBlocks()` — not needed now; OpenClaw only uses it for Copilot Claude. If provider-specific issues arise, we add it then.

**Future Work** (note in docs, don't implement):
- `VerboseLevel`, `ReasoningLevel`, `ElevatedLevel` — additional control dimensions from OpenClaw. VerboseLevel controls output detail, ReasoningLevel controls chain-of-thought visibility, ElevatedLevel controls permission escalation. Not needed for headless CUA benchmark runs but may be useful for interactive agent modes.
- `dropThinkingBlocks()` — conditional per-provider stripping of thinking blocks from history (OpenClaw: `pi-embedded-runner/thinking.ts:25-53`). Currently only needed for GitHub Copilot Claude; implement if other providers reject persisted thinking blocks on follow-up API calls. Would be part of the TranscriptPolicy pipeline (US-OC-038-040).
- `<think>` tag extraction — some models (DeepSeek R1, older reasoning models) emit reasoning inside `<think>...</think>` XML tags in plain text rather than structured thinking blocks. OpenClaw has `extractThinkingFromTaggedText()`, `splitThinkingTaggedText()`, and `promoteThinkingTagsToBlocks()` in `pi-embedded-utils.ts` to parse and promote these to structured blocks. Implement when adding non-structured reasoning models (DeepSeek R1, QwQ, etc.) — without this, thinking text stays in visible content and pollutes compaction summaries.
- Per-turn thinking level overrides — agent self-adjusting thinking depth based on task difficulty.

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

### 2. CLI: `solver.py:parse_args` (lines 95-143)

Add `--thinking-level` flag → `args["thinking_level"]`

Thread through `initialize_agent()` (lines 519-528) → `agent_kwargs["thinking_level"]`

### 3. `OpenClawAgent.__init__` (lines 35-41)

- Read `thinking_level` from kwargs (string or None)
- If provided: `ThinkingConfig(level=ThinkLevel(thinking_level))`
- If not provided: `ThinkingConfig(level=resolve_thinking_default(self.model))`

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

### 5. `run_magic_tower.sh`

Add optional `$4` for thinking level:
```bash
thinking_level="${4:-}"
THINKING_ARG=""
if [ -n "$thinking_level" ]; then
    THINKING_ARG="--thinking-level $thinking_level"
fi
# Add $THINKING_ARG to uv run command
```

### 6. Export from `__init__.py`

Add `ThinkingConfig`, `ThinkLevel`, `resolve_thinking_default` to `agents/openclaw/__init__.py`.

### Files modified (US-OC-019):
- **NEW**: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/thinking.py`
- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/__init__.py`
- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw_agent.py`
- `submodules/cua/libs/cua-bench/cua_bench/batch/solver.py`
- `run_magic_tower.sh`

---

## US-OC-020: Wire into Memory Flush and Compaction

### 1. `memory_flush.py:run_memory_flush` (line 26)

Add `thinking_params: dict | None = None` parameter. Spread `**(thinking_params or {})` into `litellm.acompletion()` at line 97.

### 2. `context.py` summarization function (line 860)

Add `thinking_params: dict | None = None` parameter. Spread into `litellm.acompletion()` at line 905.

### 3. `context.py:compact_messages` (line 1012)

Add `thinking_params: dict | None = None`. Thread to the internal summarization call.

### 4. `agent_loop.py:OpenClawComputerAgent` (line 61)

- Accept `thinking_config: ThinkingConfig | None = None` in `__init__`, store it
- In `_maybe_flush_memory()`: pass `self.thinking_config.flush_params(self.summary_model)` to `run_memory_flush`
- In `_compact_in_place()`: pass `self.thinking_config.compaction_params(self.summary_model)` to `compact_messages`

### Files modified (US-OC-020):
- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/memory_flush.py`
- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/context.py`
- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/agent_loop.py`
- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw_agent.py` (pass thinking_config)

---

## Verification

1. **Lint**: `uv run ruff check submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/`
2. **Unit tests**:
   - ThinkLevel enum values match OpenClaw's 7 levels
   - `resolve_thinking_default()`: Claude 4.6 → adaptive, reasoning model → low, unknown → off
   - `resolve_thinking_params()`: correct provider-specific output per model string
   - `ThinkingConfig.flush_params()` / `.compaction_params()` independent of main level
3. **Level 2**: `bash run_magic_tower.sh 50 anthropic/claude-sonnet-4-20250514 anthropic/claude-sonnet-4-20250514 medium` — agent runs with thinking params
4. **Level 2**: Default run (no flag) — `resolve_thinking_default` applies; for Claude Sonnet 4 → "adaptive"
