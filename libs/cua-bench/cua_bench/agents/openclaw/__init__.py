"""OpenClaw agent modules — modular components for the OpenClaw agent harness.

Components:
  - PromptBuilder: assembles structured system instructions from composable sections
  - PromptConfig / SectionConfig: section toggle configuration
  - ContextFile: bootstrap file injection container
"""

from .prompt import ContextFile, PromptBuilder, PromptConfig, SectionConfig

__all__ = ["ContextFile", "PromptBuilder", "PromptConfig", "SectionConfig"]
