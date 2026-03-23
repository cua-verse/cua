# US-OC-038/039/040: Canonical Internal Message Format — Design Investigation

## Problem

Adding GPT 5.4 support (US-OC-037) touched **7 files** and required 130 lines of format normalization code (`_normalize_messages_for_gpt54`) plus 77 lines of orphan repair (`_repair_orphaned_calls`) — all specific to one model variant within one provider. The root cause: CUA has no canonical internal message format. Messages exist in two representations at different pipeline stages, and each loop must independently convert between them.

## Current Architecture (CUA)

```
Compaction Output                    Loop Input
(role-based messages)                (provider-specific)
─────────────────────               ─────────────────────
{role: "assistant",                  OpenAI Responses API:
 content: [                            {type: "computer_call",
   {type: "text", ...},                 call_id: "...",
   {type: "computer_call", ...},        action: {...}}
   {type: "function_call", ...}
 ]}                                  Anthropic Completion:
                                       {role: "assistant",
{role: "tool",                          content: [
 content: [                               {type: "tool_use", ...}
   {type: "tool_result", ...}           ]}
 ]}
```

Each loop converts from role-based → its API format:
- **OpenAI loop**: `_normalize_messages_for_gpt54()` (130 lines) — expands content arrays into flat items, converts block types (`text` → `input_text`/`output_text`), strips `acknowledged_safety_checks`
- **OpenAI loop**: `_repair_orphaned_calls()` (77 lines) — fixes split call/result pairs in flat Responses API items
- **Anthropic loop**: tool version compat — handles `computer_20251124` vs `computer_20250124` differences
- **context.py**: `repair_tool_use_result_pairing()` (130 lines) — fixes split call/result pairs in role-based messages

Two repair functions doing the same algorithm on different data shapes.

## OpenClaw Architecture (Target Model)

```
Session File (JSONL)
       │
       ▼
sanitizeSessionHistory()          ← Single pipeline, all providers
  │  1. annotateInterSessionUserMessages
  │  2. sanitizeSessionMessagesImages
  │  3. dropThinkingBlocks
  │  4. sanitizeToolCallInputs
  │  5. repairToolUseResultPairing    ← ONE repair function
  │  6. stripToolResultDetails
  │  7. stripStaleAssistantUsage
  │  8. downgradeOpenAIReasoningBlocks
  │  9. applyGoogleTurnOrdering
       │
       ▼
  AgentMessage[]                  ← Unified format, all providers consume it
```

Key design properties:
- **One message format** (`AgentMessage`) used throughout — no format conversion needed
- **One repair function** (`repairToolUseResultPairing`) handles all cases
- **Pipeline of pure passes** — each takes `AgentMessage[]`, returns `AgentMessage[]`
- **Policy-driven** — `TranscriptPolicy` flags control which passes run (e.g., `repairToolUseResultPairing: true`, `dropThinkingBlocks: true`, `validateAnthropicTurns: true`)
- **Provider-specific passes are additive** — Google turn ordering only runs for Google; OpenAI reasoning downgrade only runs for OpenAI. No `if/else` in the loops themselves.

### How OpenClaw adds a new model: O(1) work

1. Add a `TranscriptPolicy` entry (which passes to enable)
2. If the model has a novel quirk, add one pass to the pipeline
3. No changes to any provider loop

### How CUA adds a new model today: O(n) work

1. Add/modify the loop's `predict_step()` with model-specific branches
2. Add/modify format normalization for that loop
3. Update orphan repair if the output format differs
4. Update transcript.py, memory_flush.py, context.py, tools.py for the new action/screenshot format
5. If compaction output doesn't match the new format, add conversion glue

## Source Files Examined

### OpenClaw (../openclaw/src/)

| File | Lines | Role | Key Insight |
|------|-------|------|-------------|
| `agents/session-transcript-repair.ts` | 502 | Repair orphaned tool calls + sanitize tool inputs | Single `repairToolUseResultPairing()` handles all providers. Algorithm: collect assistant call IDs → match to toolResult IDs → drop orphans/duplicates → insert synthetic results for unmatched. Skips synthesis for `stopReason === "error" \|\| "aborted"` |
| `agents/pi-embedded-runner/google.ts` | 492 | `sanitizeSessionHistory()` — the centralized pipeline | 7-pass pipeline with `TranscriptPolicy` flags. Also handles: inter-session user message annotation, stale assistant usage stripping, model snapshot tracking for cross-model switches |
| `agents/compaction.ts` | 465 | Compaction: chunking, summarization, pruning | `pruneHistoryForContextShare()` calls `repairToolUseResultPairing()` after each chunk drop — same repair function used everywhere. Constants: `BASE_CHUNK_RATIO=0.4`, `MIN_CHUNK_RATIO=0.15`, `SAFETY_MARGIN=1.2` |
| `agents/pi-embedded-runner/compact.ts` | 765 | Compaction orchestration | Session lock → repair → sanitize → limit history → repair again → compact. Calls `sanitizeToolUseResultPairing(truncated)` after `limitHistoryTurns()` because truncation can orphan tool results |

### CUA (our implementation)

| File | Lines | Role | What US-OC-038-040 changes |
|------|-------|------|----------------------------|
| `loops/openai.py` | 488 | OpenAI loop + GPT 5.4 support | `_normalize_messages_for_gpt54()` and `_repair_orphaned_calls()` → **replaced** by `sanitize_items()` call |
| `loops/anthropic.py` | ~400 | Anthropic loop | Tool version compat → **replaced** by config-driven adapter |
| `agent.py` | 906 | ComputerAgent main class | `_screenshot_output_type`, `use_safety_checks` → **driven by ModelConfig** |
| `context.py` | ~1170 | Overflow detection, compaction, repair | `repair_tool_use_result_pairing()` → **consolidated** into single repair pass |
| `agent_loop.py` | 488 | OpenClawComputerAgent | `_build_compacted_items()` → **outputs canonical items** |
| `transcript.py` | 97 | Group step output | `actions` vs `action` handling → **canonical format normalizes this** |
| `memory_flush.py` | 218 | Pre-compaction memory flush | `_serialize_content_blocks()` handles `actions` → **canonical format normalizes this** |
| `tools.py` | 179 | Tool logging | `_get_action_type_label()` handles both formats → **canonical format normalizes this** |

## Proposed Design

### Canonical Item Format

Flat items list (like Responses API, not role-based like Anthropic). Flat is strictly more expressive — role-based messages are a grouping over flat items.

```python
# openclaw/canonical.py

from typing import TypedDict, Literal, Any

class TextItem(TypedDict):
    type: Literal["text"]
    role: Literal["user", "assistant", "system"]
    text: str

class ComputerCallItem(TypedDict):
    type: Literal["computer_call"]
    call_id: str
    actions: list[dict[str, Any]]  # Always array (normalize singular → [singular])

class ComputerCallOutputItem(TypedDict):
    type: Literal["computer_call_output"]
    call_id: str
    output: dict[str, Any]  # {type: "screenshot_ref", path: "..."} or {type: "image", ...}

class FunctionCallItem(TypedDict):
    type: Literal["function_call"]
    call_id: str
    name: str
    arguments: str

class FunctionCallOutputItem(TypedDict):
    type: Literal["function_call_output"]
    call_id: str
    output: str

class CompactionSummaryItem(TypedDict):
    type: Literal["compaction_summary"]
    text: str

CanonicalItem = TextItem | ComputerCallItem | ComputerCallOutputItem | \
                FunctionCallItem | FunctionCallOutputItem | CompactionSummaryItem
```

Key decisions:
- `actions` is always an array (normalize `action: {...}` → `actions: [{...}]` at ingestion)
- Screenshot output is format-agnostic (`screenshot_ref` with path, or `image` with data) — the adapter converts to `computer_screenshot` or `input_image` based on target
- No `acknowledged_safety_checks` — added by the adapter only for models that support it
- No `role`-based grouping at the item level — role info is on TextItem only

### Adapter Pipeline

```python
# openclaw/adapter.py

def sanitize_items(
    items: list[CanonicalItem],
    target: AdapterTarget,  # "openai-responses" | "anthropic"
    config: ModelConfig,
) -> list[dict[str, Any]]:
    """Convert canonical items to provider-specific format.

    Modeled on OpenClaw's sanitizeSessionHistory — linear pipeline of pure passes.
    """
    # Pass 1: Repair orphaned call/result pairs (one function, all formats)
    items = repair_orphaned_pairs(items)

    # Pass 2: Ensure valid ordering (no trailing assistant for non-prefill models)
    items = ensure_valid_ordering(items, config)

    # Pass 3: Convert to target format
    if target == "openai-responses":
        return to_openai_responses(items, config)
    elif target == "anthropic":
        return to_anthropic_messages(items, config)

    raise ValueError(f"Unknown adapter target: {target}")
```

### Model Config Registry

```python
# openclaw/model_config.py

@dataclass
class ModelConfig:
    tool_schema_type: str          # "computer" | "computer_use_preview" | "computer_20251124"
    screenshot_output_type: str    # "computer_screenshot" | "input_image"
    supports_safety_checks: bool   # False for GPT 5.4, True for others
    action_format: str             # "batched" | "single"
    adapter_target: str            # "openai-responses" | "anthropic"

MODEL_CONFIGS = {
    "openai/gpt-5.4": ModelConfig(
        tool_schema_type="computer",
        screenshot_output_type="computer_screenshot",
        supports_safety_checks=False,
        action_format="batched",
        adapter_target="openai-responses",
    ),
    "openai/computer-use-preview": ModelConfig(
        tool_schema_type="computer_use_preview",
        screenshot_output_type="input_image",
        supports_safety_checks=True,
        action_format="single",
        adapter_target="openai-responses",
    ),
    "anthropic/claude-*": ModelConfig(
        tool_schema_type="computer_20251124",
        screenshot_output_type="input_image",
        supports_safety_checks=True,
        action_format="single",
        adapter_target="anthropic",
    ),
}
```

### Adding a new model after this work

```python
# Just add one entry:
MODEL_CONFIGS["openai/gpt-6"] = ModelConfig(
    tool_schema_type="computer_v2",
    screenshot_output_type="computer_screenshot",
    supports_safety_checks=False,
    action_format="batched",
    adapter_target="openai-responses",
)
# Zero code changes. The adapter pipeline handles conversion.
```

## Story Breakdown

### US-OC-038: Define Schema + Compaction Output (priority 6)
- Define `CanonicalItem` types in `openclaw/canonical.py`
- Modify `_build_compacted_items()` to output canonical items
- Unit tests: schema compliance, round-trip to both formats
- **Does NOT touch loops** — they still receive role-based messages for now

### US-OC-039: Centralized Adapter Pipeline (priority 7)
- Implement `sanitize_items()` pipeline with core passes: `repair_orphaned_pairs`, `ensure_valid_ordering`, format conversion
- Consolidate `repair_tool_use_result_pairing()` and `_repair_orphaned_calls()` into one `repair_orphaned_pairs()` that works on canonical items
- Delete `_normalize_messages_for_gpt54()` and `_repair_orphaned_calls()` from openai.py
- Loops call `sanitize_items()` instead of doing their own conversion
- Full regression: GPT 5.4, computer-use-preview, Anthropic models
- **NOTE**: TranscriptPolicy and thinking sanitization passes split out to US-OC-041

### US-OC-040: Model Config Registry (priority 8)
- Define `ModelConfig` dataclass and registry
- Delete `_is_gpt54()`, `get_screenshot_output_type()`, model-specific branches from loops
- Config drives the adapter pipeline and tool/screenshot handling
- Payoff test: add hypothetical `openai/gpt-6` config, verify it works with zero code changes

### US-OC-041: TranscriptPolicy + Thinking Sanitization Passes (priority 9, depends on US-OC-039)
Extends the `sanitize_items()` pipeline with provider-specific thinking sanitization:
- **TranscriptPolicy**: Dataclass with per-provider boolean flags (`drop_thinking_blocks`, `sanitize_thinking_signatures`, `downgrade_openai_reasoning`, `repair_tool_use_result_pairing`, `validate_anthropic_turns`). Resolved via `get_transcript_policy(model)`. Mirrors OpenClaw's `TranscriptPolicy` in `transcript-policy.ts`.
- **`drop_thinking_blocks` pass**: Strips `type="thinking"` content blocks from assistant messages, preserving turn structure with empty text block. Currently only needed for GitHub Copilot Claude. (OpenClaw: `pi-embedded-runner/thinking.ts:25-53`)
- **`sanitize_thinking_signatures` pass**: Strips/normalizes `thinkingSignature` fields from thinking blocks. The signature is a tamper-proof token validated by the API on re-submission — if missing, malformed, or from a different provider, the API rejects the request. Needed for cross-provider transcript replay. (OpenClaw: `transcript-policy.ts`, `google.ts`)
- **`downgrade_openai_reasoning` pass**: Converts OpenAI reasoning blocks with missing/invalid signatures to empty text blocks. Without this, o3/o4 model thinking blocks cause API rejection on replay. Also handles paired function call IDs referencing absent reasoning blocks. (OpenClaw: `pi-embedded-helpers/openai.ts:92-200`)
- All passes are no-ops when their policy flag is False — zero impact on existing providers

## Risk Assessment

- **Regression risk**: High — this refactors the core message pipeline. Mitigated by keeping all existing VM tests as regression criteria.
- **Scope creep**: Medium — tempting to also refactor transcript.py, memory_flush.py, tools.py to use canonical items. Each story should resist this and let those adapt organically.
- **OpenClaw divergence**: Intentional — we're adopting the _pattern_ (pipeline of pure passes, policy-driven) but not the exact _format_ (AgentMessage). Our CanonicalItem is flat (like Responses API), not role-based (like AgentMessage).
