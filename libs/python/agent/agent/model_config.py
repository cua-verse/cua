"""Model configuration registry — declarative format metadata per model variant.

Maps model identifiers to format metadata (tool schema type, screenshot format,
safety check support, action format, adapter target) so that adding a new model
variant requires one config entry and zero code changes.

Design reference:
  - OpenClaw's model.ts resolveModel pattern (provider catalog with format metadata)
  - CUA's @register_agent decorator (regex-based model matching)

US-OC-040: Model Config Registry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Tuple


@dataclass(frozen=True)
class ModelConfig:
    """Declarative format metadata for a model variant.

    Fields:
        tool_schema_type: OpenAI tool schema type sent to the API.
            "computer" (GPT 5.4) or "computer_use_preview" (legacy).
        screenshot_output_type: Image type in computer_call_output items.
            "computer_screenshot" (GPT 5.4, with detail="original") or
            "input_image" (computer-use-preview and Anthropic).
        supports_safety_checks: Whether to include acknowledged_safety_checks
            in computer_call_output. False for GPT 5.4.
        action_format: "batched" (GPT 5.4 actions array) or "single"
            (computer-use-preview singular action).
        adapter_target: Provider format for sanitize_items() conversion.
            "openai-responses" or "anthropic".
    """

    tool_schema_type: str
    screenshot_output_type: str
    supports_safety_checks: bool
    action_format: str
    adapter_target: str


# ---------------------------------------------------------------------------
# Registry: ordered list of (compiled_regex, ModelConfig) — first match wins.
# ---------------------------------------------------------------------------

_MODEL_CONFIGS: List[Tuple[re.Pattern, ModelConfig]] = [
    (
        re.compile(r"gpt-5\.4", re.IGNORECASE),
        ModelConfig(
            tool_schema_type="computer",
            screenshot_output_type="computer_screenshot",
            supports_safety_checks=False,
            action_format="batched",
            adapter_target="openai-responses",
        ),
    ),
    (
        re.compile(r"computer-use-preview", re.IGNORECASE),
        ModelConfig(
            tool_schema_type="computer_use_preview",
            screenshot_output_type="input_image",
            supports_safety_checks=True,
            action_format="single",
            adapter_target="openai-responses",
        ),
    ),
]

# Default config for models that don't match any pattern (Anthropic, etc.)
_DEFAULT_CONFIG = ModelConfig(
    tool_schema_type="computer_use_preview",
    screenshot_output_type="input_image",
    supports_safety_checks=True,
    action_format="single",
    adapter_target="anthropic",
)


def get_model_config(model: str) -> ModelConfig:
    """Look up model config by matching model string against registry patterns.

    Searches ``_MODEL_CONFIGS`` in order; returns the first match.  Falls back
    to ``_DEFAULT_CONFIG`` (Anthropic-compatible) if no pattern matches.

    Args:
        model: litellm model identifier (e.g. "openai/gpt-5.4", "anthropic/claude-sonnet-4-20250514").
    """
    for pattern, config in _MODEL_CONFIGS:
        if pattern.search(model):
            return config
    return _DEFAULT_CONFIG


def register_model_config(pattern: str, config: ModelConfig) -> None:
    """Register a new model config at the front of the registry.

    New entries take priority over existing ones (prepended to list).
    Useful for adding model support at runtime or in tests.

    Args:
        pattern: Regex pattern to match model strings.
        config: ModelConfig for matching models.
    """
    _MODEL_CONFIGS.insert(0, (re.compile(pattern, re.IGNORECASE), config))
