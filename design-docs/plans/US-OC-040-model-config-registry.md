# US-OC-040: Model Config Registry — Implementation Plan

## Context

US-OC-038/039 established the canonical message format and `sanitize_items()` pipeline. But model-specific differences (tool schema shape, screenshot format, safety checks, action format) are still scattered across `if _is_gpt54(model)` branches in `openai.py` and derived from loop attributes in `agent.py`. Adding a new model variant still requires modifying multiple files.

This story extracts those model-specific differences into a declarative `ModelConfig` registry so adding a new model requires **one config entry and zero code changes**.

## OpenClaw Design Rationale

### What OpenClaw Does
OpenClaw's `model.ts` (referenced in PRD) resolves model configs from a provider catalog — mapping model identifiers to provider, API format, context window, and capabilities. The `TranscriptPolicy` (from `transcript-policy.ts`) drives sanitization passes per model.

### What We Keep and Why
- **Declarative config registry** — model-specific format metadata as data, not code branches. This is the core pattern: `get_model_config(model)` returns a flat config object.
- **Regex-based model matching** — models are identified by patterns (`gpt-5\.4`, `computer-use-preview`, `claude-`), not exact strings. This mirrors OpenClaw's approach and CUA's existing `@register_agent(models=regex)`.

### What We Drop and Why
- **Provider catalog / `resolveModel()`** — OpenClaw resolves full provider metadata (API endpoints, auth). CUA uses litellm for this, so we only need format metadata.
- **TranscriptPolicy integration** — Deferred to US-OC-041. This story focuses on tool/screenshot format config only.

### Key Differences from OpenClaw
- Config lives in the CUA agent SDK (`agent/model_config.py`), not in the openclaw module — because both `openai.py` and `agent.py` need it and they're in the agent SDK package.
- Flat `ModelConfig` dataclass with 5 fields (vs OpenClaw's richer model resolution). Kept simple per PRD notes.

## Implementation Plan

### Step 1: Create `agent/model_config.py`

**File**: `submodules/cua/libs/python/agent/agent/model_config.py`

```python
@dataclass(frozen=True)
class ModelConfig:
    tool_schema_type: str          # "computer" | "computer_use_preview"
    screenshot_output_type: str    # "computer_screenshot" | "input_image"
    supports_safety_checks: bool   # False for GPT 5.4, True for others
    action_format: str             # "batched" | "single"
    adapter_target: str            # "openai-responses" | "anthropic"

# Registry: list of (compiled_regex, ModelConfig) tuples
_MODEL_CONFIGS: list[tuple[re.Pattern, ModelConfig]] = [
    (re.compile(r"gpt-5\.4", re.IGNORECASE), ModelConfig(
        tool_schema_type="computer",
        screenshot_output_type="computer_screenshot",
        supports_safety_checks=False,
        action_format="batched",
        adapter_target="openai-responses",
    )),
    (re.compile(r"computer-use-preview", re.IGNORECASE), ModelConfig(
        tool_schema_type="computer_use_preview",
        screenshot_output_type="input_image",
        supports_safety_checks=True,
        action_format="single",
        adapter_target="openai-responses",
    )),
    # Default fallback for Anthropic / unknown models
]

def get_model_config(model: str) -> ModelConfig:
    """Look up model config by matching model string against registry patterns."""

def register_model_config(pattern: str, config: ModelConfig) -> None:
    """Register a new model config (for extensibility and testing)."""
```

### Step 2: Modify `openai.py` — replace model-specific functions with config lookup

**File**: `submodules/cua/libs/python/agent/agent/loops/openai.py`

Changes:
- **Delete** `_is_gpt54()` (lines 22-24)
- **Delete** `get_screenshot_output_type()` (lines 70-76)
- **Modify** `_map_computer_tool_to_openai()` — use `config.tool_schema_type` instead of `_is_gpt54()`
- **Modify** `_prepare_tools_for_openai()` — pass config instead of model string
- **Modify** `predict_step()` — get config once, pass to helpers, set `self.screenshot_output_type` from config
- **Modify** `predict_click()` — use config.tool_schema_type instead of `_is_gpt54()`

The class attribute `screenshot_output_type` stays — `agent.py` already reads it via `getattr()`. It's just set from config instead of from `get_screenshot_output_type()`.

### Step 3: Modify `agent.py` — derive safety checks from config

**File**: `submodules/cua/libs/python/agent/agent/agent.py`

Changes:
- **Line 318-320**: `_screenshot_output_type` initialization stays the same (reads from loop attribute). No change needed here — the loop now sets it correctly from config.
- **Line 599**: `use_safety_checks` derivation stays the same (`!= "computer_screenshot"`). This is already correct — it derives from the loop's screenshot_output_type which is now config-driven.

**Result**: `agent.py` needs **no changes**. The config flows through the loop's `screenshot_output_type` attribute, which `agent.py` already reads.

### Step 4: Modify `agent_loop.py` — use config for compaction target

**File**: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/agent_loop.py`

Changes:
- **Line 418**: Replace hardcoded `target="openai-responses"` with config lookup:
  ```python
  from agent.model_config import get_model_config
  config = get_model_config(self.model)
  compacted_items = sanitize_items(canonical_messages, target=config.adapter_target)
  ```

### Step 5: Wire exports

**File**: `submodules/cua/libs/python/agent/agent/__init__.py`
- Export `ModelConfig`, `get_model_config`, `register_model_config`

**File**: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/__init__.py`
- Re-export if needed (probably not — direct import from `agent.model_config` is cleaner)

### Step 6: Unit tests

**File**: `tests/test_model_config.py`

Tests:
1. `get_model_config('openai/gpt-5.4')` returns correct GPT 5.4 config
2. `get_model_config('openai/computer-use-preview')` returns correct CUP config
3. `get_model_config('anthropic/claude-sonnet-4-20250514')` returns correct Anthropic config
4. `get_model_config('openai/gpt-5.4-turbo')` also matches GPT 5.4 pattern
5. **Payoff test**: `register_model_config('gpt-6', ...)` + `sanitize_items()` produces valid output with zero code changes
6. `_is_gpt54` and `get_screenshot_output_type` no longer importable from openai.py

### Files Modified

| File | Change |
|------|--------|
| `agent/model_config.py` | **NEW** — ModelConfig dataclass + registry + get_model_config() |
| `agent/loops/openai.py` | Delete _is_gpt54, get_screenshot_output_type; use config lookup |
| `agent/agent.py` | No changes needed (reads screenshot_output_type from loop) |
| `agent_loop.py` | Use config.adapter_target for compaction |
| `agent/__init__.py` | Export new symbols |
| `openclaw/__init__.py` | Optional re-export |
| `tests/test_model_config.py` | **NEW** — unit tests |

## Verification

1. **Level 1**: `uv run ruff check .` passes
2. **Level 1**: Unit tests pass (`uv run pytest tests/test_model_config.py -v`)
3. **Level 1**: Verify `_is_gpt54` and `get_screenshot_output_type` are deleted
4. **Level 1**: Payoff test — hypothetical gpt-6 config works with zero code changes
5. **Level 2**: `bash run_magic_tower.sh 15 openai/gpt-5.4` passes (regression)
6. **Level 2**: `bash run_magic_tower.sh 15 openai/computer-use-preview` passes (regression)
