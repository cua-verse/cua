# US-OC-050–054: Unified Agent Loop (pi-ai Pattern)

## Problem

CUA has separate per-provider loops (`anthropic.py`: 1684 LOC, `openai.py`: 266 LOC) that each independently handle API calls, request formatting, and response parsing. This causes:

1. **Reasoning traces silently dropped** — `_convert_completion_to_responses_items()` in the Anthropic loop ignores `thinking_blocks` and `reasoning_content` from litellm responses
2. **Code duplication** — tool formatting, action mapping, usage extraction reimplemented per-provider
3. **Fragile extensibility** — adding a new provider or response field requires modifying each loop independently
4. **Anthropic loop bloat** — 437 lines of manual Chat Completions → Responses API conversion that litellm can do natively

## OpenClaw Design Rationale

### What OpenClaw Does (pi-ai pattern)

OpenClaw uses a **single unified agent loop** (`createAgentSession().prompt()`) with provider differences handled through composable stream function wrappers:

```
StreamFn → wrap(anthropic-headers) → wrap(openai-reasoning) → wrap(google-thinking) → API
API → pi-ai normalizes response → unified extractAssistantThinking() → output
```

Key components:
- `StreamFn` type — pluggable transport abstraction
- `streamWithPayloadPatch()` — intercepts and mutates outgoing payloads per-provider
- `applyExtraParamsToAgent()` — ordered chain: pre-plugin wrappers → plugin hooks → post-plugin wrappers
- `pi-ai` core — handles response normalization internally; all providers return unified `AgentMessage` format

### What We Keep and Why

1. **One loop, all providers** — eliminates the class of bugs where one loop handles a field and another doesn't (the reasoning trace bug). Provider differences are in request/response adapters, not loop logic.

2. **Composable request patching** — OpenClaw's `streamWithPayloadPatch` pattern (mutate outgoing payload per-provider). We adapt this as Python dict patchers instead of streaming function wrappers.

3. **ResolvedModel-driven dispatch** — OpenClaw's `resolveModel()` determines provider behavior. We already have `ResolvedModel` (US-OC-047) — we extend it to drive the unified loop.

### What We Drop and Why

1. **Streaming function composition** — pi-ai wraps `StreamFn` because it's a streaming architecture. CUA uses request/response (not streaming), so simple dict patching suffices.

2. **Plugin system** — OpenClaw has `wrapProviderStreamFn` hooks for third-party providers. CUA doesn't need this; the `ModelConfig` registry (US-OC-040) is sufficient.

3. **pi-ai as a dependency** — pi-ai is a TypeScript library; we use litellm as our transport abstraction instead.

### Key Differences from OpenClaw

- **Transport**: litellm (`aresponses`/`acompletion`) instead of pi-ai's `StreamFn`
- **Request patching**: Dict mutation pipeline instead of streaming wrapper composition
- **Response normalization**: litellm's `LiteLLMCompletionResponsesConfig` + CUA computer-action mapping instead of pi-ai's internal normalizer
- **Configuration**: `ResolvedModel` dataclass instead of OpenClaw's `resolveModel()` async function

## Architecture

### Current State (per-provider loops)

```
ComputerAgent.run()
    │
    ├─ model matches "claude-*" → AnthropicHostedToolsConfig.predict_step()
    │   ├── _prepare_tools_for_anthropic()
    │   ├── _convert_responses_items_to_completion_messages()  [525 lines]
    │   ├── litellm.acompletion()
    │   ├── _convert_completion_to_responses_items()           [437 lines, DROPS REASONING]
    │   └── manual usage extraction
    │
    └─ model matches "gpt-5.4|computer-use-preview" → OpenAIComputerUseConfig.predict_step()
        ├── _prepare_tools_for_openai()
        ├── litellm.aresponses()
        ├── response.model_dump()  [passthrough, reasoning included]
        └── response.usage.model_dump()
```

### Target State (unified loop)

```
ComputerAgent.run()
    │
    └─ UnifiedAgentConfig.predict_step()
        │
        ├── 1. resolve_model(model)
        │     → ResolvedModel { provider, model_api, tool_schema_type, ... }
        │
        ├── 2. build_request_kwargs(resolved, messages, tools, **kwargs)   [US-OC-051]
        │     ├── base_kwargs(resolved)           → model, messages/input, stream, retries
        │     ├── patch_tool_schemas(resolved)     → provider-specific tool format
        │     ├── patch_anthropic(resolved)        → beta headers, cache_control
        │     ├── patch_openai(resolved)           → reasoning defaults, truncation
        │     └── patch_thinking(resolved, kwargs) → merge thinking params
        │
        ├── 3. call_api(resolved, api_kwargs)
        │     → litellm.aresponses() or litellm.acompletion() based on resolved.model_api
        │
        ├── 4. normalize_response(resolved, response)                     [US-OC-052]
        │     ├── aresponses: response.model_dump() (passthrough)
        │     ├── acompletion: LiteLLMCompletionResponsesConfig.transform() 
        │     │    + computer_action_mapping()
        │     ├── extract_reasoning()  ← built into both paths
        │     └── extract_usage()
        │
        └── 5. return {"output": items, "usage": usage}
```

## Story Breakdown

### US-OC-050: Spike — Verify aresponses() for Anthropic (Priority 10)

**Goal**: Determine whether `litellm.aresponses()` works for Anthropic with computer-use tools + thinking.

**Key questions**:
1. Does `aresponses()` accept Anthropic's `computer_20251124` tool schema?
2. Does the response include computer-use actions in the output items?
3. Do reasoning/thinking traces appear in the output?
4. Does prompt caching work through `aresponses()`?

**Outputs**: 
- Spike script with results
- Decision: `aresponses()` for all providers, or `acompletion()` fallback for Anthropic
- If fallback needed: verify `LiteLLMCompletionResponsesConfig.transform_chat_completion_response_to_responses_api_response()` handles computer-use actions

**Fallback plan**: If `aresponses()` doesn't support Anthropic computer-use, the unified loop uses `acompletion()` + litellm's built-in response transformation for Anthropic. The architecture stays the same — only the transport method differs per-provider, controlled by `ResolvedModel.model_api`.

### US-OC-051: Request Builder (Priority 11)

**New file**: `submodules/cua/libs/python/agent/agent/request_builder.py`

```python
def build_request_kwargs(
    resolved: ResolvedModel,
    messages: list[dict],
    tools: list[dict],
    computer_handler: Any,
    *,
    use_prompt_caching: bool = False,
    **generation_kwargs,
) -> dict[str, Any]:
    """Assemble provider-ready API kwargs from ResolvedModel metadata."""
    
    kwargs = _base_kwargs(resolved, messages, generation_kwargs)
    kwargs["tools"] = _format_tools(resolved, tools, computer_handler)
    
    # Provider-specific patches (no-op for non-matching providers)
    _patch_anthropic(resolved, kwargs, use_prompt_caching)
    _patch_openai(resolved, kwargs)
    
    return kwargs
```

**Absorbs from old code**:
- `MODEL_TOOL_MAPPING` → merged into `ModelConfig` registry entries (add `tool_version`, `beta_flag` fields)
- `_prepare_tools_for_anthropic()` → `_format_tools()` anthropic branch
- `_prepare_tools_for_openai()` → `_format_tools()` openai branch
- Beta header injection → `_patch_anthropic()`
- `_add_cache_control()` / `_combine_completion_messages()` → `_patch_anthropic()` prompt caching branch
- Hardcoded `reasoning`/`truncation` → `_patch_openai()`

### US-OC-052: Response Normalizer (Priority 12)

**New file**: `submodules/cua/libs/python/agent/agent/response_normalizer.py`

```python
def normalize_response(
    resolved: ResolvedModel,
    response: Any,
    api_method: str,  # "aresponses" or "acompletion"
) -> dict[str, Any]:
    """Normalize any provider response to {'output': items, 'usage': dict}."""
    
    if api_method == "aresponses":
        return _normalize_aresponses(response)  # passthrough + usage
    else:
        return _normalize_acompletion(resolved, response)  # litellm transform + action mapping
```

**Key design decision**: Computer action mapping (click, type, scroll, etc.) is the one piece litellm's transform can't handle. Extract the ~150 lines of action-type mapping from `_convert_completion_to_responses_items()` into a focused `_map_computer_actions()` helper. Everything else (text, tool_calls, reasoning) goes through litellm's built-in transform.

### US-OC-053: Unified Loop (Priority 13)

**New file**: `submodules/cua/libs/python/agent/agent/loops/unified.py`

```python
@register_agent(models=r".*", priority=100)  # High priority, catches all
class UnifiedAgentConfig(AsyncAgentConfig):
    async def predict_step(self, messages, model, tools=None, **kwargs):
        resolved = resolve_model(model)
        api_kwargs = build_request_kwargs(resolved, messages, tools, **kwargs)
        
        if resolved.model_api == "responses":
            response = await litellm.aresponses(**api_kwargs)
        else:
            response = await litellm.acompletion(**api_kwargs)
        
        return normalize_response(resolved, response, resolved.model_api)
```

**Coexistence strategy**: Register with high priority so it matches first. Old loops remain importable for rollback — just lower the unified loop's priority or unregister it.

### US-OC-054: Cleanup (Priority 14)

Delete or gut the old loop classes after US-OC-053 VM tests pass:
- `AnthropicHostedToolsConfig` — delete class + 9 helper functions
- `OpenAIComputerUseConfig` — delete class + 2 helper functions
- `MODEL_TOOL_MAPPING` — absorbed into ModelConfig registry
- Keep `_convert_responses_items_to_completion_messages()` only if used by other consumers

## Dependencies

```
US-OC-047 (ResolvedModel)  ──────────────────────┐
US-OC-040 (ModelConfig registry)  ────────────────┤
US-OC-039 (sanitize_items input pipeline)  ───────┤
                                                   ▼
                                            US-OC-050 (spike)
                                                   │
                                          ┌────────┴────────┐
                                          ▼                 ▼
                                    US-OC-051          US-OC-052
                                    (request)          (response)
                                          │                 │
                                          └────────┬────────┘
                                                   ▼
                                            US-OC-053
                                          (unified loop)
                                                   │
                                                   ▼
                                            US-OC-054
                                            (cleanup)
```

## US-OC-050 Spike Results (2026-04-09)

### Findings

**Q1: Does `aresponses()` work for Anthropic with computer-use tools?**
**NO.** Fails with `KeyError: 'function'` in litellm's Anthropic tool mapping.

Root cause traced through litellm internals:
1. `aresponses()` has no native Anthropic Responses API — falls back to `acompletion()` + transformation
2. `LiteLLMCompletionResponsesConfig._transform_responses_api_tools_to_chat_completion_tools()` passes `computer_use_preview` tools through as-is (line 1307: catch-all `else` branch)
3. Anthropic's `_map_tool_helper()` expects `tool["function"]` format for `computer_*` tools (line 403) → KeyError

This is a litellm limitation: the Responses→Chat Completions tool transform doesn't know how to convert Responses API computer-use tool schemas to Anthropic's hosted tool format.

**Q2: Does `aresponses()` work for OpenAI with computer-use tools?**
**YES.** Returns correct `computer_call` output items with actions. Response shape:
```json
{
  "id": "cu_...",
  "type": "computer_call",
  "status": "completed",
  "actions": [{"type": "screenshot"}],
  "call_id": "call_..."
}
```
Note: GPT-5.4 uses `actions` (array, batched), computer-use-preview uses `action` (singular).

**Q3: Do reasoning/thinking traces appear?**
Not tested for Anthropic (blocked by Q1). OpenAI reasoning was 0 tokens for the simple test prompt — would need a more complex scenario.

**Q4: Does prompt caching work through `aresponses()`?**
Not tested (blocked by Q1 for Anthropic). Not applicable for OpenAI.

### Phase 1 Decision (Direct API)

**Dual transport**: `acompletion()` for Anthropic, `aresponses()` for OpenAI.

---

### Phase 2: OpenRouter Spike (2026-04-09)

Team decision to switch from direct Anthropic/OpenAI APIs to **OpenRouter** as the unified provider gateway. Re-ran spike with `openrouter/` model prefix.

**Q1: Do function-calling computer tools work via OpenRouter?**
**YES, for both providers.** Since OpenRouter proxies via Chat Completions API, native hosted computer-use tools (`computer_20251124`, `computer_use_preview`) are NOT supported. Instead, computer actions use standard **function calling** — a `computer` function tool with action/x/y/text params. This matches upstream CUA's new GPT-5.4 function-calling approach.

Both Anthropic and OpenAI return `function_call` items with correct arguments:
```json
{"type": "function_call", "name": "computer", "arguments": "{\"action\": \"click\", \"x\": 10, \"y\": 1070}"}
```

**Q2: Does reasoning work via OpenRouter?**
**YES via `acompletion()`** with OpenRouter's unified `reasoning` param (not Anthropic's `thinking`):
- Both providers accept `reasoning={'effort': 'medium'}` — OpenRouter translates `effort` into the appropriate `budget_tokens` for Anthropic internally
- Anthropic: returns `reasoning_content` + `reasoning_details` with `reasoning.text` type and `signature`
- OpenAI: returns `reasoning_content` + `reasoning_details` with `reasoning.summary` and `reasoning.encrypted` types
- **Single interface**: `reasoning={'effort': level}` works for all providers — no need to branch on `max_tokens` vs `effort`

**NO via `aresponses()`** — litellm's acompletion→Responses transform drops `reasoning_content`/`reasoning_details` from the Chat Completions response.

**Q3: Which transport method to use?**
`acompletion()` is the clear winner for OpenRouter:
- Both providers go through the same Chat Completions API
- Reasoning is preserved (returned in `reasoning_content`)
- Function tools work uniformly
- `aresponses()` adds no value (falls back to acompletion anyway) and loses reasoning

**Q4: OpenRouter model IDs?**
Different from direct API model strings:
- `openrouter/anthropic/claude-sonnet-4` (not `anthropic/claude-sonnet-4-20250514`)
- `openrouter/openai/gpt-5.4`

### Phase 2 Decision (OpenRouter)

**Single transport: `litellm.acompletion()` for all providers via OpenRouter.**

This dramatically simplifies the unified loop:
- ONE API call method for all providers (no aresponses vs acompletion dispatch)
- ONE tool format for all providers (function calling, no native hosted tools)
- Reasoning via `reasoning` param (OpenRouter unified format)
- Response normalization is simpler — all responses are Chat Completions format

### Impact on US-OC-051/052

- **US-OC-051 (Request Builder)**: Simpler — single kwarg shape for all providers. Uses `messages` + Chat Completions function tools. `reasoning={'effort': level}` works uniformly for all providers via OpenRouter.
- **US-OC-052 (Response Normalizer)**: Simpler — all responses are Chat Completions format. Extract `reasoning_content`/`reasoning_details` from message. Computer actions come as `function_call` tool_calls (parse `arguments` JSON). No native `computer_call` action mapping needed.
- **US-OC-053 (Unified Loop)**: Much simpler — single `acompletion()` call, no model_api dispatch.
- **ModelConfig/ResolvedModel**: Need updates for OpenRouter model IDs and `model_api="chat"` for all.

### Spike artifacts

- `tests/spike_aresponses_anthropic.py` — spike script (Phase 1 + Phase 2 notes)

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| `aresponses()` doesn't support Anthropic computer-use tools | **CONFIRMED** | Low | Fallback activated: `acompletion()` for Anthropic. Architecture unchanged. |
| litellm's response transform misses CUA-specific fields | Medium | Low | Layer CUA-specific mapping on top of litellm's transform. |
| Anthropic prompt caching doesn't work through `aresponses()` | Medium | Medium | Keep `acompletion()` for Anthropic if caching is critical; controlled by `ResolvedModel.model_api`. |
| Edge cases in action mapping (triple_click, mouse_down/up) | Low | Low | Extract action mapping from old code; same logic, different location. |
| Old loop consumers (callbacks, trajectory savers) assume specific output shape | Low | Medium | Unified loop returns identical `{"output": items, "usage": dict}` format. |

## Metrics

**Before** (current):
- 1,950 LOC across two loops (1,684 + 266)
- Reasoning traces dropped for Anthropic
- Adding a provider = writing a new loop class

**After** (target):
- ~300 LOC unified loop + ~200 LOC request builder + ~200 LOC response normalizer
- Reasoning traces preserved for all providers
- Adding a provider = one `ModelConfig` registry entry
