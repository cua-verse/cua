"""PromptBuilder — modular system prompt assembly for the OpenClaw agent harness.

Design rationale: docs/plan/US-OC-001-system-prompt-builder.md
Reference implementation: openclaw/src/agents/system-prompt.ts (buildAgentSystemPrompt)
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ContextFile:
    """A file to inject into the Project Context section.

    Follows OpenClaw's contextFiles bootstrap injection pattern.
    """

    path: str  # Display label ("AGENTS.md", "task.md")
    content: str  # Full content to inject


@dataclass
class SectionConfig:
    """Toggle for an individual prompt section."""

    enabled: bool = True


@dataclass
class PromptConfig:
    """Configuration for which prompt sections to include."""

    identity: SectionConfig = field(default_factory=SectionConfig)
    tools: SectionConfig = field(default_factory=SectionConfig)
    memory: SectionConfig = field(default_factory=SectionConfig)
    project_context: SectionConfig = field(default_factory=SectionConfig)


class PromptBuilder:
    """Assembles structured system instructions from composable sections.

    Sections (in order):
      1. Identity — one-line agent role
      2. Tools — registered tool names with descriptions
      3. Memory Recall — when/how to use memory tools (only if memory tools present)
      4. Project Context — bootstrap injection (AGENTS.md, task.md, etc.)
    """

    def __init__(self, config: PromptConfig | None = None) -> None:
        self.config = config or PromptConfig()

    def build(
        self,
        *,
        tool_summaries: dict[str, str] | None = None,
        context_files: list[ContextFile] | None = None,
    ) -> str:
        """Assemble all enabled sections into a single prompt string."""
        parts: list[str] = []

        if self.config.identity.enabled:
            parts.extend(self._build_identity())

        if self.config.tools.enabled and tool_summaries:
            parts.extend(self._build_tools(tool_summaries))

        if self.config.memory.enabled and tool_summaries:
            memory_lines = self._build_memory(tool_summaries)
            if memory_lines:
                parts.extend(memory_lines)

        if self.config.project_context.enabled and context_files:
            parts.extend(self._build_project_context(context_files))

        return "\n".join(parts)

    def _build_identity(self) -> list[str]:
        """Build the Identity section."""
        return [
            "## Identity",
            "",
            (
                "You are an AI agent running inside the AgentHLE benchmark framework. "
                "Your role is to complete computer-use tasks on a remote Windows desktop "
                "by observing screenshots and performing mouse/keyboard actions."
            ),
            "",
        ]

    def _build_tools(self, tool_summaries: dict[str, str]) -> list[str]:
        """Build the Tools section listing registered tools.

        Tool order reflects caller's dict insertion order (Python 3.7+ stable).
        """
        lines = ["## Tools", "", "You have access to the following tools:", ""]
        for name, description in tool_summaries.items():
            lines.append(f"- **{name}**: {description}")
        lines.append("")
        return lines

    def _build_memory(self, tool_summaries: dict[str, str]) -> list[str]:
        """Build the Memory Recall section. Only included if memory tools are present.

        Mirrors OpenClaw's buildMemorySection() in system-prompt.ts:
        search-first behavioral directive, not a generic tutorial.
        """
        memory_tools = {"memory_search", "memory_get"}
        if not memory_tools.intersection(tool_summaries):
            return []

        return [
            "## Memory Recall",
            (
                "Before acting on anything about prior attempts, strategies, environment "
                "observations, or task state: run memory_search on MEMORY.md + memory/*.md; "
                "then use memory_get to pull only the needed lines. If low confidence after "
                "search, say you checked."
            ),
            "",
        ]

    def _build_project_context(self, context_files: list[ContextFile]) -> list[str]:
        """Build the Project Context section with injected file contents.

        # TODO US-OC-008: add per-file and total char size caps
        # (ref: openclaw bootstrap 20K/150K limits)
        """
        if not context_files:
            return []

        lines = [
            "# Project Context",
            "",
            "The following project context files have been loaded:",
            "",
        ]
        for cf in context_files:
            lines.append(f"## {cf.path}")
            lines.append("")
            lines.append(cf.content)
            lines.append("")
        return lines
