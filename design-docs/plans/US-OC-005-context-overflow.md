# US-OC-005: Context Overflow Detection

## Context

The agent harness needs to detect when the context window is nearing its limit so that compaction (US-OC-006) can be triggered before the LLM API rejects the request. OpenClaw handles this reactively (catching API errors) and proactively (token estimation + tool result truncation). We implement the proactive path using CUA's `on_llm_start` callback, which is strictly better — it prevents wasted API calls.

This story includes full wiring into the agent loop — the callback is active and logging during runs, and its `needs_compaction` flag is available for the compaction pipeline (US-OC-006) to consume.

---

## OpenClaw Design Rationale

**What OpenClaw Does**: Estimates tokens via `chars/4`, applies a 1.2x safety margin, truncates tool results exceeding 30% of context window (head+tail strategy preserving error output), and triggers compaction on overflow.

**What We Keep**: `chars/4` estimation, `SAFETY_MARGIN = 1.2`, tool result truncation with head+tail strategy and truncation suffix, proactive pre-LLM-call detection.

**What We Change**: OpenClaw uses 30% share for tool results; PRD specifies 25% — we use 25%. OpenClaw detects reactively from API errors; we detect proactively in `on_llm_start`. OpenClaw's truncation modifies the session file; we truncate in-memory only.

**What We Add**: Reactive error detection as a fallback — `is_context_overflow_error(error_message)` adapted from OpenClaw's `isLikelyContextOverflowError` to catch cases where proactive detection underestimates and the API still rejects the request.

**What We Drop**: Retry loop (US-OC-006), `sessionLikelyHasOversizedToolResults` heuristic.

---

## Implementation

### New file: `agents/openclaw/context.py`

**Constants** (from OpenClaw reference):
- `SAFETY_MARGIN = 1.2`
- `DEFAULT_CONTEXT_TOKENS = 200_000`
- `FIXED_IMAGE_TOKENS = 1200` (standard API cost for a 1024x768 screenshot)
- `MAX_TOOL_RESULT_SHARE = 0.25` (25% of context window)
- `HARD_MAX_TOOL_RESULT_CHARS = 400_000` (safety net from OpenClaw)
- `MIN_KEEP_CHARS = 2_000`
- `TRUNCATION_SUFFIX` (warning text matching OpenClaw)
- `MIDDLE_OMISSION_MARKER` (for head+tail truncation)

#### Context window resolution

Use `litellm.get_model_info(model)` at runtime — it has a comprehensive model registry:

```python
def resolve_context_window(model: str) -> int:
    """Resolve context window tokens for a model via litellm's model registry."""
    try:
        import litellm
        info = litellm.get_model_info(model)
        max_input = info.get("max_input_tokens")
        if max_input and max_input > 0:
            return max_input
    except Exception:
        pass
    # Also try stripping provider prefix (e.g. "anthropic/claude-..." -> "claude-...")
    if "/" in model:
        try:
            import litellm
            info = litellm.get_model_info(model.split("/", 1)[1])
            max_input = info.get("max_input_tokens")
            if max_input and max_input > 0:
                return max_input
        except Exception:
            pass
    return DEFAULT_CONTEXT_TOKENS
```

Verified: `litellm.get_model_info("anthropic/claude-sonnet-4-20250514")` returns `max_input_tokens: 1_000_000`. Falls back to `DEFAULT_CONTEXT_TOKENS = 200_000` for unknown models.

#### Token estimation functions

- `estimate_message_tokens(msg: dict) -> int` — `len(json.dumps(msg)) // 4`, with special image handling: detect `computer_call_output` items with `data:image/` URLs, subtract base64 string length, add `FIXED_IMAGE_TOKENS` per image
- `estimate_messages_tokens(msgs: list[dict]) -> int` — sum over messages

#### Tool result truncation (adapted from `openclaw/src/agents/pi-embedded-runner/tool-result-truncation.ts`)

- `has_important_tail(text: str) -> bool` — detect error/summary patterns in last 2000 chars
- `truncate_tool_result_text(text: str, max_chars: int) -> str` — head+tail strategy when tail has errors/summaries, head-only otherwise
- `truncate_tool_results(msgs: list[dict], context_window: int) -> list[dict]` — truncate `function_call_output` items with oversized `output` strings (CUA format)

#### Reactive error detection (adapted from OpenClaw's `isLikelyContextOverflowError`)

```python
# Regex patterns from openclaw/src/agents/pi-embedded-helpers/errors.ts
_CONTEXT_OVERFLOW_PATTERNS = [
    r"request_too_large",
    r"context length exceeded",
    r"prompt is too long",
    r"exceeds model context window",
    r"request size exceeds",
    r"maximum context length",
    r"context overflow",
    r"too many tokens",
    r"content_too_large",
]
_RATE_LIMIT_EXCLUDE = re.compile(r"rate.?limit|tpm|tpd|rpm|rpd", re.IGNORECASE)

def is_context_overflow_error(error_message: str) -> bool:
    """Detect if an API error was caused by context window overflow.

    Adapted from OpenClaw's isLikelyContextOverflowError (errors.ts).
    Excludes rate limit false positives.
    """
    if not error_message:
        return False
    if _RATE_LIMIT_EXCLUDE.search(error_message):
        return False
    lower = error_message.lower()
    return any(p in lower for p in _CONTEXT_OVERFLOW_PATTERNS)
```

The agent loop in `openclaw_agent.py` wraps the API call and checks: if an exception matches `is_context_overflow_error`, set `overflow_cb.force_compaction()` to signal US-OC-006.

#### ContextOverflowCallback class

```python
class ContextOverflowCallback(AsyncCallbackHandler):
    def __init__(
        self,
        context_window: int | None = None,
        threshold: float = 0.80,
        model: str = "",
        instructions_tokens: int = 0,  # offset for system prompt added after us in callback chain
    ):
        self._context_window = context_window or resolve_context_window(model)
        self._threshold = threshold
        self._instructions_tokens = instructions_tokens
        self._current_tokens = 0
        self._turn_count = 0
        self._needs_compaction = False

    # Properties: current_tokens, context_window, needs_compaction, overflow_ratio

    def force_compaction(self) -> None:
        """Force needs_compaction=True (called by agent loop on reactive overflow detection)."""
        self._needs_compaction = True

    async def on_llm_start(self, messages):
        self._turn_count += 1
        messages = truncate_tool_results(messages, self._context_window)
        raw = estimate_messages_tokens(messages)
        self._current_tokens = int(raw * SAFETY_MARGIN) + self._instructions_tokens
        self._needs_compaction = self._current_tokens > self._context_window * self._threshold
        print(f"[ContextOverflow] turn {self._turn_count}: ~{self._current_tokens // 1000}K/{self._context_window // 1000}K tokens ({self.overflow_ratio:.0%}), needs_compaction={self._needs_compaction}")
        return messages
```

### Callback ordering

With `callbacks=[overflow_callback]`, the final order in ComputerAgent:
1. OperatorNormalizerCallback (prepended by CUA)
2. **ContextOverflowCallback** (ours)
3. PromptInstructionsCallback (appended by CUA from `instructions=`)
4. ImageRetentionCallback (appended by CUA from `only_n_most_recent_images=`)

Our callback sees messages BEFORE image stripping — conservative (overestimates), which is safer. The `instructions_tokens` offset accounts for the system prompt injected after us.

### Wiring in `openclaw_agent.py` (full — not deferred)

In `perform_task()`, after building the prompt and before creating ComputerAgent:

```python
from .openclaw import ContextOverflowCallback, is_context_overflow_error

overflow_cb = ContextOverflowCallback(
    model=self.model,
    instructions_tokens=len(instructions) // 4,
)

agent = ComputerAgent(
    model=self.model,
    tools=tools,
    only_n_most_recent_images=3,
    trajectory_dir=trajectory_dir,
    instructions=instructions,
    callbacks=[overflow_cb],
)
```

In the agent loop, two detection paths:

```python
# Proactive: after each step, check the flag
if overflow_cb.needs_compaction:
    print(f"[ContextOverflow] Compaction needed at step {step}")
    # US-OC-006 will add: await compact(session_mgr, overflow_cb, ...)

# Reactive: wrap the agent.run() iteration in try/except
try:
    async for result in agent.run(instruction):
        ...
except Exception as e:
    if is_context_overflow_error(str(e)):
        overflow_cb.force_compaction()
        print(f"[ContextOverflow] API rejected — overflow: {e}")
        # US-OC-006 will add retry-after-compact logic
    else:
        raise
```

---

## Files Changed

| File | Action |
|------|--------|
| `submodules/cua/.../agents/openclaw/context.py` | **NEW** — ContextOverflowCallback + token estimation + truncation + is_context_overflow_error |
| `submodules/cua/.../agents/openclaw/__init__.py` | **MODIFY** — export ContextOverflowCallback, is_context_overflow_error |
| `submodules/cua/.../agents/openclaw_agent.py` | **MODIFY** — wire callback + check needs_compaction in loop |
| `tests/test_openclaw_context.py` | **NEW** — ~25-30 unit tests |
| `docs/plan/US-OC-005-context-overflow.md` | **NEW** — this file |

## Testing Support

`CONTEXT_WINDOW_OVERRIDE` env var — when set, `openclaw_agent.py` passes the integer value as `context_window=` to `ContextOverflowCallback`, overriding litellm model lookup. Useful for triggering overflow detection in short runs:

```bash
CONTEXT_WINDOW_OVERRIDE=8000 bash run_magic_tower.sh 15
```

## Design Note: Context Accumulation

Within a single `agent.run()` call, CUA accumulates messages (`old_items + new_items`) and sends the full history to the LLM each turn — context grows linearly. Our callback correctly tracks this growth.

**Cross-session** transcript replay (loading prior run's messages as `old_items`) is NOT implemented in this story. `SessionManager.load_history()` exists (US-OC-004) but `perform_task()` doesn't call it yet. This wiring is deferred to **US-OC-008** (Agent Loop Integration), which has an explicit Level 3 acceptance criterion: "session 2 loads session 1's transcript." Until then, cross-session context comes only through memory (TASK_MEMORY.md bootstrap injection).

Once US-OC-008 wires transcript replay, context overflow detection becomes critical — accumulated transcripts can easily exceed the context window, making compaction (US-OC-006) essential for multi-session tasks.

## Verification

1. **Level 1**: `uv run ruff check .` passes
2. **Level 1**: `uv run pytest tests/test_openclaw_context.py -v` — all tests pass
3. **Level 1**: Tests cover: chars/4 estimation, image token substitution, safety margin, litellm context window resolution, threshold triggering, tool result truncation (head-only and head+tail), truncation suffix, callback properties, turn counting, is_context_overflow_error (true positives, rate limit exclusions, empty input), force_compaction()
4. **Level 2**: `run_magic_tower.sh 50` — callback logs `[ContextOverflow]` per turn with token estimates, estimates within 2x of actual usage
