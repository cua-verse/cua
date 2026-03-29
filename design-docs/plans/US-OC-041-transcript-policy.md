# US-OC-041: TranscriptPolicy + Thinking Sanitization Passes

## Context

The `sanitize_items()` pipeline (US-OC-039) currently runs repair + ordering + format conversion passes but has no provider-specific thinking sanitization. Different providers have different requirements for thinking blocks:
- Anthropic Claude can reject replayed thinking blocks with invalid/missing signatures
- OpenAI Responses API rejects orphaned reasoning items without their paired function_call
- Some providers (GitHub Copilot Claude) reject thinking blocks entirely

OpenClaw solves this with a `TranscriptPolicy` dataclass that drives per-provider sanitization passes. This story reproduces that pattern for CUA.

## OpenClaw Design Rationale

### What OpenClaw Does
- `TranscriptPolicy` type in `transcript-policy.ts`: 12+ boolean flags resolved per-provider via `resolveTranscriptPolicy({modelApi, provider, modelId})`
- `dropThinkingBlocks()` in `pi-embedded-runner/thinking.ts`: strips `type="thinking"` blocks, preserves turn structure with empty text block
- `downgradeOpenAIReasoningBlocks()` in `pi-embedded-helpers/openai.ts`: drops thinking blocks that have a valid OpenAI reasoning signature but no following non-thinking block (orphaned reasoning)
- `downgradeOpenAIFunctionCallReasoningPairs()`: rewrites dual-ID tool call IDs when reasoning is absent
- Pipeline in `sanitizeSessionHistory()` (google.ts:577): resolves policy, then conditionally applies passes based on flags

### What We Keep and Why
- **TranscriptPolicy dataclass with boolean flags** — simple, extensible, no class hierarchy needed
- **`drop_thinking_blocks` pass** — direct port of `dropThinkingBlocks()`, needed for providers that reject thinking
- **`sanitize_thinking_signatures` pass** — strips `thinkingSignature` from blocks for cross-provider replay
- **`downgrade_openai_reasoning` pass** — handles orphaned OpenAI reasoning blocks that cause API rejection
- **Policy resolved per-model** — `get_transcript_policy(model)` using the same litellm model string pattern as `thinking.py`
- **Passes are no-ops when flag is False** — zero impact on current providers

### What We Drop and Why
- **`sanitizeToolCallIds`/`toolCallIdMode`** — not needed; CUA doesn't do cross-provider tool ID rewriting
- **`sanitizeMode` (full vs images-only)** — image sanitization is handled separately in `session.py`
- **`applyGoogleTurnOrdering`/`validateGeminiTurns`** — Google-specific turn validation lives in the CUA Gemini loop
- **`preserveSignatures`/`sanitizeThoughtSignatures`** — Gemini-specific thought signature handling, not applicable
- **`allowSyntheticToolResults`** — currently handled by `repair_orphaned_pairs(synthesize=True)` at sanitize-time. However, write-time guard is the better long-term approach (see Deferred Work below).
- **`downgradeOpenAIFunctionCallReasoningPairs`** — requires dual-ID format (`call_XXX|fc_XXX`) which we don't implement yet; deferred to new story

### Key Differences from OpenClaw
- OpenClaw resolves policy from `{modelApi, provider, modelId}` (3 params); we resolve from a single litellm model string (same pattern as `thinking.py`)
- OpenClaw's `shouldDropThinkingBlocksForModel` uses provider capability hints; we use simple model string matching
- We integrate policy into `sanitize_items()` directly rather than having it as a separate `sanitizeSessionHistory()` wrapper

## Implementation Plan

### File: `canonical.py` (extend existing)

**1. Add `TranscriptPolicy` dataclass** (after `COMPACTION_PREAMBLE`):
```python
@dataclass(frozen=True)
class TranscriptPolicy:
    drop_thinking_blocks: bool = False
    sanitize_thinking_signatures: bool = False
    downgrade_openai_reasoning: bool = False
    repair_tool_use_result_pairing: bool = True
    validate_anthropic_turns: bool = True
```

**2. Add `get_transcript_policy(model)` function**:
- Detect provider from litellm model string (same pattern as `thinking.py:resolve_thinking_params`)
- Anthropic: `validate_anthropic_turns=True`, `drop_thinking_blocks=True` (signatures may be invalid on replay)
- OpenAI: `downgrade_openai_reasoning=True`
- Default: all thinking passes disabled
- Note: currently all passes default to safe no-ops for existing providers. `drop_thinking_blocks=True` for Anthropic is forward-looking — the Anthropic adapter already skips thinking blocks in `canonical_to_anthropic_messages`, so the pass is redundant but correct.

**3. Add three sanitization pass functions**:

`drop_thinking_blocks(messages)`:
- Iterate assistant messages, remove `type="thinking"` blocks
- If all blocks removed, replace with `[TextBlock(type="text", text="")]` to preserve turn structure
- Return original list if nothing changed (reference equality optimization)

`sanitize_thinking_signatures(messages)`:
- Iterate all thinking blocks, remove `thinkingSignature` key
- Needed for cross-provider replay where signatures are provider-specific

`downgrade_openai_reasoning(messages)`:
- For assistant messages, find thinking blocks with a valid OpenAI reasoning signature (JSON with `id` and `type` fields)
- If the thinking block has no following non-thinking block in the same message, drop it (orphaned reasoning)
- Preserves thinking blocks that have following content (they're part of a valid call chain)

**4. Extend `sanitize_items()` signature**:
```python
def sanitize_items(
    messages: list[CanonicalMessage],
    target: Literal["openai-responses", "anthropic"],
    *,
    policy: TranscriptPolicy | None = None,
) -> list[dict[str, Any]]:
```
- If `policy` is None, resolve from target (anthropic → Anthropic policy, openai-responses → OpenAI policy)
- Apply thinking passes between repair and format conversion:
  1. `repair_orphaned_pairs` (existing)
  2. `drop_thinking_blocks` (if `policy.drop_thinking_blocks`)
  3. `sanitize_thinking_signatures` (if `policy.sanitize_thinking_signatures`)
  4. `downgrade_openai_reasoning` (if `policy.downgrade_openai_reasoning`)
  5. `ensure_valid_ordering` (existing)
  6. Format conversion (existing)

### File: `tests/test_canonical_format.py` (extend existing)

Add test cases for:
- `TranscriptPolicy` defaults
- `get_transcript_policy()` returns correct flags per provider
- `drop_thinking_blocks`: strips thinking, preserves turn with empty text, no-op when no thinking
- `sanitize_thinking_signatures`: removes `thinkingSignature` field, preserves `thinking` text
- `downgrade_openai_reasoning`: drops orphaned reasoning with valid signature, keeps reasoning with following content
- All passes are no-ops when flag is False
- `sanitize_items()` with explicit policy overrides

### Files NOT modified
- `model_config.py` — policy is resolved from model string, not stored in ModelConfig (keeps concerns separate)
- `thinking.py` — ThinkingConfig is about API parameters, not transcript sanitization
- `agent_loop.py` — already calls `sanitize_items()`, will get policy support automatically via default resolution

## Verification

1. **Level 1**: `uv run ruff check submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/canonical.py tests/test_canonical_format.py`
2. **Level 1**: `uv run pytest tests/test_canonical_format.py -v` — all new + existing tests pass
3. **Level 1**: `uv run pytest tests/ -v` — full test suite regression
4. **Level 2**: Existing VM tests pass unchanged (passes are no-ops for current model configs)

## Deferred Work: New PRD Stories

After US-OC-041 implementation, create via `/prd`:

### Story: Cross-Provider Tool Call ID Sanitization
- Port OpenClaw's `sanitizeToolCallIdsForCloudCodeAssist()` — rewrite tool call IDs to be provider-compatible
- **Reference**: `openclaw/src/agents/tool-call-id.ts`
- **Depends on**: US-OC-041, US-OC-032

### Story: OpenAI Dual-ID Format + Reasoning Pair Downgrade
- Implement dual-ID tool call format (`call_XXX|fc_YYY`) + `downgradeOpenAIFunctionCallReasoningPairs()`
- **Reference**: `openclaw/src/agents/pi-embedded-helpers/openai.ts:67-200`
- **Depends on**: US-OC-041

### Story: Write-Time Tool Result Guard (Session Transcript Integrity)
- Port OpenClaw's `session-tool-result-guard.ts` — write-time guard over sanitize-time repair
- Write-time is preferred: long-lived sessions, readable transcripts, crash recovery
- **Reference**: `openclaw/src/agents/session-tool-result-guard.ts`
- **Depends on**: US-OC-041
