# US-OC-046: TranscriptPolicy Parity - Runtime Model-Resolved Policy

## Context

`US-OC-041` introduced `TranscriptPolicy`, but the runtime still had two architectural gaps:

1. policy resolution in production was still effectively target-based instead of live-model-based
2. the normal send path in `OpenClawComputerAgent.run()` did not go through the canonical `sanitize_items()` pipeline; only compaction rebuild did

That left AgentHLE in a split-brain state:
- replay and compaction paths partially used canonical sanitization
- the main per-turn send path still forwarded raw history
- helper `litellm.acompletion()` paths reused the same thinking kwargs shape as the OpenAI Responses runtime, even when that transport was incompatible

This story restores the OpenClaw invariant that transcript policy is resolved from the active model/runtime and applied on replay/rebuild surfaces and on runtime paths that still need normalization, not only on rebuild paths.

## OpenClaw Design Rationale

### What OpenClaw Does

- `transcript-policy.ts` resolves policy from live model metadata (`modelApi`, `provider`, `modelId`), not merely from an output target
- the runtime send path sanitizes history before provider calls
- helper paths use model-aware runtime adapters instead of blindly forwarding one provider's reasoning payload shape everywhere
- `sanitizeMode` communicates whether a provider needs broad transcript normalization or only image-focused cleanup

### What We Keep and Why

- **Model-resolved transcript policy**: AgentHLE should resolve replay/send behavior from the actual model string in the same way it resolves thinking behavior
- **`sanitize_mode` on `TranscriptPolicy`**: even if current Python passes are still flag-driven, the intent should exist in the policy object so the runtime mirrors OpenClaw's shape
- **One sanitize policy owner across runtime/replay/compaction**: avoids the current inconsistency where rebuilt history is safer than normal live history, while still preserving provider-native in-run item streams when rewriting them would lose semantics
- **Split thinking intent from transport mapping**: the main OpenAI Responses loop can use `reasoning`, but helper chat-completions style calls must not assume the same payload is legal

### What We Drop and Why

- **Full OpenClaw provider catalog**: not needed yet; AgentHLE still resolves from the litellm model string rather than a richer resolver object
- **Every OpenClaw policy flag**: keep only the flags relevant to the currently supported providers and failure modes
- **Cross-session reasoning reconstruction**: this story makes session reasoning behavior explicit, but it does not attempt a full OpenAI reasoning replay reconstruction system

### Key Differences from OpenClaw

- OpenClaw resolves from structured model metadata; AgentHLE currently resolves from the litellm model string plus `ModelConfig.adapter_target`
- OpenClaw has a broader runtime/provider matrix; AgentHLE currently focuses on Anthropic, OpenAI Responses, and Gemini-compatible behavior
- For helper calls, AgentHLE currently takes the pragmatic path for GPT-5.4: suppress unsupported `reasoning` kwargs on `litellm.acompletion()` instead of inventing a second OpenAI helper transport

## Implementation Plan

### 1. Extend `TranscriptPolicy` to carry sanitize intent

File: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/canonical.py`

- add `sanitize_mode: Literal["full", "images-only"]`
- keep current pass flags (`drop_thinking_blocks`, `sanitize_thinking_signatures`, `downgrade_openai_reasoning`, etc.)
- make `get_transcript_policy(model)` return:
  - Anthropic/Claude: `sanitize_mode="full"`
  - OpenAI/GPT/o-series: `sanitize_mode="images-only"`
  - Gemini/Google/Vertex: `sanitize_mode="full"`

Rationale: this restores the OpenClaw-style policy shape and makes the current provider intent explicit, even before every possible pass is ported.

### 2. Resolve sanitize policy from the live model in production paths

File: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/canonical.py`

- extend `sanitize_items()` to accept `model=`
- if `policy` is not provided and `model` is set:
  - resolve policy via `get_transcript_policy(model)`
- if `target` is omitted and `model` is set:
  - derive `target` from `agent.model_config.get_model_config(model).adapter_target`

Rationale: `sanitize_items()` should become callable from runtime paths with just the live model, so callers do not silently drift back to target-only defaults.

### 3. Apply model-aware sanitize at the runtime boundary without rewriting native live items

File: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/agent_loop.py`

- add a private helper:
  - normalize raw runtime messages to canonical via `normalize_to_canonical()`
  - run `sanitize_items(..., model=self.model)`
- call this helper in `OpenClawComputerAgent.run()` after `replace_failed_computer_calls_with_function_calls()` and before `_on_llm_start()` / `predict_step()`
- if the current history is already in provider-native flat Responses item format, preserve it as-is instead of canonicalizing and re-emitting it
- switch compaction rebuild to use the same model-aware sanitize path rather than manually resolving only `adapter_target`

Rationale: the runtime should still resolve one policy owner from the live model, but native in-run OpenAI Responses items must not be downgraded into transcript-style text placeholders. VM testing showed that re-canonicalizing native `computer_call` / `computer_call_output` items on every turn breaks computer-state continuity and causes repeated screenshots. So the correct boundary is:
- replay / transcript-derived / compaction-rebuilt history -> canonical sanitize
- already-native live Responses item streams -> preserve in place

### 4. Split thinking level intent from transport mapping

File: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/thinking.py`

- extend `resolve_thinking_params()` with `transport="responses" | "chat"`
- keep the main loop behavior:
  - OpenAI Responses main runtime -> `{"reasoning": {...}}`
- change helper behavior:
  - GPT-5.4 / OpenAI helper `litellm.acompletion()` calls -> suppress `reasoning` kwargs (`{}`) for now
- update:
  - `ThinkingConfig.to_api_params()` -> `transport="responses"`
  - `ThinkingConfig.flush_params()` -> `transport="chat"`
  - `ThinkingConfig.compaction_params()` -> `transport="chat"`

Rationale: the main loop and helper calls are different transports. Reusing one provider-specific payload shape across both caused the GPT-5.4 helper-call failure this story owns.

### 5. Keep helper paths aligned with the centralized adapter

Files:
- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/memory_flush.py`
- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/context.py`
- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/agent_loop.py`

- no helper-specific policy forks
- memory flush continues to take `thinking_params` from `ThinkingConfig.flush_params()`
- compaction summarization continues to take `thinking_params` from `ThinkingConfig.compaction_params()`
- the important change is that these params are now transport-aware rather than blindly mirroring the Responses runtime

## Session Reasoning Decision

For this story, the explicit product decision is:

- keep normalized `thinking` blocks in canonical/session-side representations when they exist
- apply provider/model-aware sanitization on replay and transcript-derived send surfaces
- do not promise that raw provider-native reasoning payloads can be replayed unchanged across transports

This keeps transcript fidelity work from US-OC-020/041 while making the replay boundary explicit.

## Verification

### Level 1

- `python3 -m compileall submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/canonical.py submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/thinking.py submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/agent_loop.py`
- `UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_thinking.py tests/test_canonical_format.py tests/test_openclaw_memory_flush.py tests/test_openclaw_compaction.py`

Expected coverage:
- policy resolution differs by model without manual injection
- runtime sanitize helper applies model-resolved policy to transcript-style history and preserves already-native flat Responses items
- helper call sites no longer forward GPT-5.4/OpenAI `reasoning` kwargs into `litellm.acompletion()`

### Level 2

- `bash run_magic_tower.sh 50 openai/gpt-5.4`
- `bash run_magic_tower.sh 50 anthropic/claude-sonnet-4-20250514`

Checks:
- OpenAI helper paths do not fail with unknown reasoning parameter errors
- runtime send path continues to preserve valid turn ordering and native computer-call continuity
- transcript replay remains usable after the run

## Notes

This story intentionally stops short of `US-OC-047`'s richer model resolver. If future work adds structured model metadata beyond `ModelConfig`, `get_transcript_policy()` should migrate to that resolver rather than keep parsing raw model strings independently.

Implementation note after VM validation:
- The first draft of this story applied canonical sanitize too aggressively to the live OpenAI Responses stream. That was corrected after VM evidence showed repeated screenshot loops. The implemented design keeps the model-aware policy central, but narrows canonical sanitize to replay/rebuild/transcript-derived paths and preserves already-native live Responses items.

## Current Status

This story is code-complete, but final closure is still blocked by one external Level 2 validation issue.

Already landed in the current checkpoint:
- `TranscriptPolicy` now carries `sanitize_mode`
- `sanitize_items()` can resolve from `model=...`
- the main loop has a model-aware runtime sanitize boundary
- native live OpenAI Responses item streams are preserved instead of being blindly re-canonicalized
- helper thinking params are now transport-aware
- memory flush has an OpenAI Responses helper path instead of always using chat-completions style helpers
- focused Level 1 tests cover policy resolution, helper thinking behavior, transcript formatting, solver wiring, and analyze-image thinking-param forwarding
- OpenAI/GPT-5.4 Level 2 validation reached live turns, memory flush, and compaction successfully without the old helper `reasoning` transport failure
- session transcripts now retain normalized `thinking` blocks with `thinkingSignature` on the OpenAI path

## Remaining Work

### 1. Clear the Anthropic Level 2 validation blocker

The remaining blocker is no longer a known `046` runtime/parity issue. The required Anthropic VM run currently fails before meaningful agent execution due to an environment/dependency problem in LiteLLM/tiktoken:

- `ValueError: Duplicate encoding name gpt2 in tiktoken plugin tiktoken_ext.openai_public 3`

This occurs on the Anthropic path before the run can exercise the strict-provider transcript-policy behavior that `046` is intended to validate.

### 2. Re-run the Anthropic VM gate after the environment fix

Once the LiteLLM/tiktoken environment issue is resolved, re-run:

- `bash run_magic_tower.sh 50 anthropic/claude-sonnet-4-20250514`

The verification target is unchanged:
- transcript send path uses the same runtime sanitize pipeline
- no turn-ordering regression on a stricter provider
- no helper transport mismatch caused by model-resolved policy

### 3. Keep adjacent replay hardening in their own stories unless the re-run disproves that split

`US-OC-043` to `US-OC-045` remain separate stories. If the Anthropic re-run shows that `046` still depends on tool-call ID sanitization, OpenAI dual-ID downgrade behavior, or the write-time tool-result guard, that ownership split should be revisited with concrete evidence rather than assumptions.
