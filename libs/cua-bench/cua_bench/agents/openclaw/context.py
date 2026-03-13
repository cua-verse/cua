"""Context overflow detection and tool result truncation for the OpenClaw agent harness.

Proactive detection: ContextOverflowCallback runs in CUA's on_llm_start callback chain,
estimating token usage and truncating oversized tool results before the API call.

Reactive detection: is_context_overflow_error() catches API rejections when proactive
detection underestimates.

Reference implementation:
  - openclaw/src/agents/pi-embedded-runner/tool-result-truncation.ts — truncation logic
  - openclaw/src/agents/pi-embedded-helpers/errors.ts — error classification
  - openclaw/src/agents/compaction.ts — context budgeting constants
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from agent.callbacks.base import AsyncCallbackHandler

# ---------------------------------------------------------------------------
# Constants (from OpenClaw reference)
# ---------------------------------------------------------------------------

SAFETY_MARGIN = 1.2
"""Multiply raw token estimate by this factor to absorb tokenizer variance."""

DEFAULT_CONTEXT_TOKENS = 200_000
"""Fallback when the model's context window can't be resolved."""

FIXED_IMAGE_TOKENS = 1200
"""Standard API cost for a 1024x768 screenshot (Anthropic billing)."""

MAX_TOOL_RESULT_SHARE = 0.25
"""Maximum share of context window a single tool result may occupy (PRD: 25%)."""

HARD_MAX_TOOL_RESULT_CHARS = 400_000
"""Absolute character cap for a single tool result (OpenClaw safety net)."""

MIN_KEEP_CHARS = 2_000
"""Minimum characters to preserve when truncating."""

TRUNCATION_SUFFIX = (
    "\n\n\u26a0\ufe0f [Content truncated \u2014 original was too large for the model's "
    "context window. The content above is a partial view. If you need more, "
    "request specific sections or use offset/limit parameters to read smaller chunks.]"
)

MIDDLE_OMISSION_MARKER = (
    "\n\n\u26a0\ufe0f [... middle content omitted \u2014 showing head and tail ...]\n\n"
)


# ---------------------------------------------------------------------------
# Context window resolution
# ---------------------------------------------------------------------------

def resolve_context_window(model: str) -> int:
    """Resolve context window tokens for a model via litellm's model registry.

    Falls back to DEFAULT_CONTEXT_TOKENS for unknown models.
    """
    for candidate in _model_candidates(model):
        try:
            import litellm
            info = litellm.get_model_info(candidate)
            max_input = info.get("max_input_tokens")
            if max_input and max_input > 0:
                return int(max_input)
        except Exception:
            continue
    return DEFAULT_CONTEXT_TOKENS


def _model_candidates(model: str) -> list[str]:
    """Yield model name variants to try (full name, then without provider prefix)."""
    candidates = [model]
    if "/" in model:
        candidates.append(model.split("/", 1)[1])
    return candidates


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

# Matches base64 image data URLs in computer_call_output content
_BASE64_IMAGE_RE = re.compile(r'"data:image/[^;]+;base64,[A-Za-z0-9+/=]+"')


def estimate_message_tokens(msg: dict[str, Any]) -> int:
    """Estimate token count for a single message using chars/4 heuristic.

    Special handling for images: subtracts the base64 string length and adds
    FIXED_IMAGE_TOKENS per image (matching actual API billing).
    """
    raw = json.dumps(msg, separators=(",", ":"))
    # Count and subtract base64 image data, replace with fixed token cost
    image_count = 0
    base64_chars = 0
    for match in _BASE64_IMAGE_RE.finditer(raw):
        image_count += 1
        base64_chars += len(match.group())
    char_tokens = (len(raw) - base64_chars) // 4
    return char_tokens + (image_count * FIXED_IMAGE_TOKENS)


def estimate_messages_tokens(msgs: list[dict[str, Any]]) -> int:
    """Estimate total token count for a list of messages."""
    return sum(estimate_message_tokens(m) for m in msgs)


# ---------------------------------------------------------------------------
# Tool result truncation
# ---------------------------------------------------------------------------

_IMPORTANT_TAIL_RE = re.compile(
    r"\b(error|exception|failed|fatal|traceback|panic|stack trace|errno|exit code)\b",
    re.IGNORECASE,
)
_SUMMARY_TAIL_RE = re.compile(
    r"\b(total|summary|result|complete|finished|done)\b",
    re.IGNORECASE,
)


def has_important_tail(text: str) -> bool:
    """Detect error/summary patterns in the last 2000 chars of text."""
    tail = text[-2000:]
    if _IMPORTANT_TAIL_RE.search(tail):
        return True
    # JSON closing structure
    if re.search(r"\}\s*$", tail.strip()):
        return True
    if _SUMMARY_TAIL_RE.search(tail):
        return True
    return False


def truncate_tool_result_text(text: str, max_chars: int) -> str:
    """Truncate a single text string to fit within max_chars.

    Uses head+tail strategy when the tail contains important content (errors,
    results, JSON), otherwise preserves the beginning.

    Adapted from openclaw/src/agents/pi-embedded-runner/tool-result-truncation.ts
    """
    if len(text) <= max_chars:
        return text

    budget = max(MIN_KEEP_CHARS, max_chars - len(TRUNCATION_SUFFIX))

    # Head+tail when tail looks important
    if has_important_tail(text) and budget > MIN_KEEP_CHARS * 2:
        tail_budget = min(int(budget * 0.3), 4_000)
        head_budget = budget - tail_budget - len(MIDDLE_OMISSION_MARKER)

        if head_budget > MIN_KEEP_CHARS:
            # Find clean cut points at newline boundaries
            head_cut = head_budget
            head_newline = text.rfind("\n", 0, head_budget)
            if head_newline > head_budget * 0.8:
                head_cut = head_newline

            tail_start = len(text) - tail_budget
            tail_newline = text.find("\n", tail_start)
            if tail_newline != -1 and tail_newline < tail_start + int(tail_budget * 0.2):
                tail_start = tail_newline + 1

            return text[:head_cut] + MIDDLE_OMISSION_MARKER + text[tail_start:] + TRUNCATION_SUFFIX

    # Default: keep the beginning
    cut_point = budget
    last_newline = text.rfind("\n", 0, budget)
    if last_newline > budget * 0.8:
        cut_point = last_newline
    return text[:cut_point] + TRUNCATION_SUFFIX


def _calculate_max_tool_result_chars(context_window: int) -> int:
    """Max allowed chars for a single tool result given the context window."""
    max_tokens = int(context_window * MAX_TOOL_RESULT_SHARE)
    max_chars = max_tokens * 4  # chars/4 heuristic inverse
    return min(max_chars, HARD_MAX_TOOL_RESULT_CHARS)


def truncate_tool_results(
    msgs: list[dict[str, Any]], context_window: int
) -> list[dict[str, Any]]:
    """Truncate oversized function_call_output items in a message list (in-memory).

    CUA format: items with type=function_call_output have an "output" string field.
    Returns a new list — does not mutate the original.
    """
    max_chars = _calculate_max_tool_result_chars(context_window)
    result: list[dict[str, Any]] = []
    for msg in msgs:
        if msg.get("type") == "function_call_output":
            output = msg.get("output", "")
            if isinstance(output, str) and len(output) > max_chars:
                msg = copy.copy(msg)
                msg["output"] = truncate_tool_result_text(output, max_chars)
        result.append(msg)
    return result


# ---------------------------------------------------------------------------
# Reactive error detection
# ---------------------------------------------------------------------------

_CONTEXT_OVERFLOW_PATTERNS = [
    "request_too_large",
    "context length exceeded",
    "prompt is too long",
    "exceeds model context window",
    "request size exceeds",
    "maximum context length",
    "context overflow",
    "too many tokens",
    "content_too_large",
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


# ---------------------------------------------------------------------------
# ContextOverflowCallback
# ---------------------------------------------------------------------------

class ContextOverflowCallback(AsyncCallbackHandler):
    """Pre-LLM callback that estimates token usage and truncates oversized tool results.

    Wired into the CUA agent via ``callbacks=[overflow_cb]``. Runs before
    PromptInstructionsCallback and ImageRetentionCallback in the chain, so it sees
    messages before image stripping (conservative — overestimates, which is safer).

    After each ``on_llm_start``, check ``needs_compaction`` to decide whether to
    trigger the compaction pipeline (US-OC-006).
    """

    def __init__(
        self,
        context_window: int | None = None,
        threshold: float = 0.80,
        model: str = "",
        instructions_tokens: int = 0,
    ):
        self._context_window = context_window or resolve_context_window(model)
        self._threshold = threshold
        self._instructions_tokens = instructions_tokens
        self._current_tokens = 0
        self._turn_count = 0
        self._needs_compaction = False

    # -- Public read-only properties --

    @property
    def current_tokens(self) -> int:
        """Estimated token count after the last on_llm_start call."""
        return self._current_tokens

    @property
    def context_window(self) -> int:
        """Resolved context window size in tokens."""
        return self._context_window

    @property
    def needs_compaction(self) -> bool:
        """Whether estimated usage exceeds the threshold."""
        return self._needs_compaction

    @property
    def overflow_ratio(self) -> float:
        """Current tokens as a fraction of the context window."""
        if self._context_window <= 0:
            return 0.0
        return self._current_tokens / self._context_window

    @property
    def turn_count(self) -> int:
        """Number of on_llm_start calls so far."""
        return self._turn_count

    # -- Mutation --

    def force_compaction(self) -> None:
        """Force needs_compaction=True (called by agent loop on reactive overflow detection)."""
        self._needs_compaction = True

    # -- Callback --

    async def on_llm_start(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Estimate tokens, truncate oversized tool results, set needs_compaction flag."""
        self._turn_count += 1
        messages = truncate_tool_results(messages, self._context_window)
        raw = estimate_messages_tokens(messages)
        self._current_tokens = int(raw * SAFETY_MARGIN) + self._instructions_tokens
        self._needs_compaction = (
            self._current_tokens > self._context_window * self._threshold
        )
        print(
            f"[ContextOverflow] turn {self._turn_count}: "
            f"~{self._current_tokens // 1000}K/{self._context_window // 1000}K tokens "
            f"({self.overflow_ratio:.0%}), needs_compaction={self._needs_compaction}"
        )
        return messages
