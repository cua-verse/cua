# US-OC-054: Finalize Unified Loop (Keep Old Files)

## Context

US-OC-051 shipped the unified loop (`unified.py`) that routes all providers through OpenRouter via single `acompletion()` + function-calling tools. The old per-provider loops (`anthropic.py`, `openai.py`) still exist and work for direct API calls. The user wants to **finalize** the unified loop as the primary path without deleting the old files.

The original US-OC-054 acceptance criteria call for deletion. We adapt the scope: make the unified loop the default, add tests, ensure lint, and leave old loops as fallback.

## Auxiliary LLM call sites

Three auxiliary calls bypass the agent loop and call litellm directly:
- **Compaction** — `call_helper_model()` in `openclaw/helper_runtime.py` → `litellm.acompletion()` or `.aresponses()` based on `helper_transport_defaults`
- **Memory flush** — same path as compaction, via `call_helper_model(purpose="memory_flush")`
- **Image analysis** — `AnalyzeImageTool._execute()` → direct `litellm.acompletion(model=self.model)`

All three use `summary_model` (defaults to main model if not set). Switching to `openrouter/` prefix passes through to litellm unchanged — no issues.

## What changes

### 1. Default model → OpenRouter prefix
**File**: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw_agent.py`
- Change default model from `anthropic/claude-sonnet-4-20250514` to `openrouter/anthropic/claude-sonnet-4-20250514`
- This makes all default runs route through `UnifiedAgentConfig` instead of `AnthropicHostedToolsConfig`
- `summary_model` defaults to main model via `kwargs.get("summary_model", None) or self.model`, so it inherits the openrouter prefix automatically

### 2. Default model in run_magic_tower.sh
**File**: `run_magic_tower.sh`
- Update default `model_id` to `openrouter/anthropic/claude-sonnet-4-20250514`

### 3. Unit tests for unified loop
**New file**: `tests/test_unified_loop.py`
- Test `_convert_input_to_messages()` — Responses API items → Chat Completions messages
- Test `_convert_response_to_output()` — Chat Completions response → Responses API output items
- Test `_prepare_tools()` — CUA tool schemas → Chat Completions function tools
- Test reasoning extraction (reasoning_content → reasoning output item)
- Test legacy format handling (computer_call/computer_call_output conversion)

### 4. Lint check
- Run `uv run ruff check .` on the submodule and fix any issues in unified.py

### 5. Update PRD acceptance criteria
- Adapt US-OC-054 criteria to reflect "finalize" scope instead of "delete"
- Mark story as passes=true after verification

## What we do NOT change
- `anthropic.py` — kept as-is (fallback for direct Anthropic API)
- `openai.py` — kept as-is (fallback for direct OpenAI API)
- `loops/__init__.py` — already imports `unified`, no change needed
- `MODEL_TOOL_MAPPING` — stays in anthropic.py (only used internally, no need to absorb since we're keeping the file)

## Verification
1. `uv run ruff check .` — lint passes
2. `uv run pytest tests/test_unified_loop.py -v` — unit tests pass
3. `bash run_magic_tower.sh 15` — default model routes through unified loop (OpenRouter)
4. `bash run_magic_tower.sh 15 openrouter/openai/gpt-5.4` — OpenAI via unified loop
5. `bash run_magic_tower.sh 15 anthropic/claude-sonnet-4-20250514` — old Anthropic loop still works as fallback
