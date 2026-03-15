"""OpenClaw agent modules — modular components for the OpenClaw agent harness.

Components:
  - PromptBuilder: assembles structured system instructions from composable sections
  - PromptConfig / SectionConfig: section toggle configuration
  - ContextFile: bootstrap file injection container
  - MemoryStore / SearchResult: task-workspace persistent memory storage
  - MemorySearchTool / MemoryGetTool / MemoryWriteTool: agent memory tools
  - SessionManager / SessionState / TokenUsage / TranscriptEntry: session persistence
  - ContextOverflowCallback / is_context_overflow_error: context overflow detection (US-OC-005)
  - CompactionResult / compact_messages: compaction pipeline (US-OC-006)
  - build_tools / get_tool_summaries / ToolLoggingCallback: tool registry & logging (US-OC-007)
"""

from .context import (
    CompactionResult,
    ContextOverflowCallback,
    compact_messages,
    is_context_overflow_error,
)
from .memory import (
    MemoryGetTool,
    MemorySearchTool,
    MemoryStore,
    MemoryWriteTool,
    SearchResult,
)
from .prompt import ContextFile, PromptBuilder, PromptConfig, SectionConfig
from .session import (
    SessionManager,
    SessionState,
    TokenUsage,
    TranscriptEntry,
    build_system_prompt_report,
)
from .tools import ToolLoggingCallback, build_tools, get_tool_summaries

__all__ = [
    "CompactionResult",
    "ContextFile",
    "ContextOverflowCallback",
    "ToolLoggingCallback",
    "build_tools",
    "compact_messages",
    "get_tool_summaries",
    "MemoryGetTool",
    "MemorySearchTool",
    "MemoryStore",
    "MemoryWriteTool",
    "PromptBuilder",
    "PromptConfig",
    "SearchResult",
    "SectionConfig",
    "SessionManager",
    "SessionState",
    "TokenUsage",
    "TranscriptEntry",
    "build_system_prompt_report",
    "is_context_overflow_error",
]
