"""PromptBuilder — modular system prompt assembly for the OpenClaw agent harness.

Design rationale: docs/plan/US-OC-001-system-prompt-builder.md
Reference implementation: openclaw/src/agents/system-prompt.ts (buildAgentSystemPrompt)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class ContextFile:
    """A file to inject into the Project Context section.

    Follows OpenClaw's contextFiles bootstrap injection pattern.
    """

    path: str  # Display label ("AGENTS.md", "TASK_MEMORY.md")
    content: str  # Full content to inject


@dataclass
class SectionConfig:
    """Toggle for an individual prompt section."""

    enabled: bool = True


@dataclass
class PromptConfig:
    """Configuration for which prompt sections to include."""

    identity: SectionConfig = field(default_factory=SectionConfig)
    time: SectionConfig = field(default_factory=SectionConfig)
    tools: SectionConfig = field(default_factory=SectionConfig)
    memory: SectionConfig = field(default_factory=SectionConfig)
    delegation: SectionConfig = field(default_factory=SectionConfig)
    gui_interaction: SectionConfig = field(default_factory=SectionConfig)
    project_context: SectionConfig = field(default_factory=SectionConfig)


class PromptBuilder:
    """Assembles structured system instructions from composable sections.

    Sections (in order, matching OpenClaw's system-prompt.ts):
      1. Identity — one-line agent role
      2. Tools — registered tool names with descriptions
      3. Memory Recall — when/how to use memory tools (only if memory tools present)
      4. Delegation — subagent delegation prose (only if delegation tools present)
      5. GUI Interaction — direct-click/keyboard bullets (only if main computer
         can drive the VM interactively, i.e. main_computer_interactive=True)
      6. Current Date & Time — UTC timestamp (ref: OpenClaw system-prompt.ts)
      7. Project Context — bootstrap injection (AGENTS.md, TASK_MEMORY.md, etc.)

    Feature-gated sections (delegation, gui_interaction) mirror OpenClaw's
    buildAgentSystemPrompt pattern: absence is the signal — when a tool isn't
    available, its prose isn't emitted, and the model is not told "X is
    disabled."
    """

    def __init__(self, config: PromptConfig | None = None) -> None:
        self.config = config or PromptConfig()

    def build(
        self,
        *,
        tool_summaries: dict[str, str] | None = None,
        context_files: list[ContextFile] | None = None,
        main_computer_interactive: bool = True,
    ) -> str:
        """Assemble all enabled sections into a single prompt string.

        Args:
            tool_summaries: Name -> description mapping for registered tools.
                Drives the Tools section and the conditional inclusion of
                Memory Recall / Delegation subsections.
            context_files: Bootstrap files injected into the Project Context
                section (AGENTS.md, optionally TASK_MEMORY.md).
            main_computer_interactive: True when the main ``computer`` tool
                can perform interactive actions (click/type/scroll/drag/move).
                False when the tool is restricted to screenshot + wait (see
                ``_RestrictedComputerHandler`` under ``disable_main_computer``).
                Cannot be inferred from ``tool_summaries`` alone because both
                the full and restricted handler expose ``name="computer"``.
        """
        parts: list[str] = []

        if self.config.identity.enabled:
            parts.extend(self._build_identity())

        if self.config.tools.enabled and tool_summaries:
            parts.extend(self._build_tools(tool_summaries))

        if self.config.memory.enabled and tool_summaries:
            memory_lines = self._build_memory(tool_summaries)
            if memory_lines:
                parts.extend(memory_lines)

        if self.config.delegation.enabled and tool_summaries:
            delegation_lines = self._build_delegation(tool_summaries)
            if delegation_lines:
                parts.extend(delegation_lines)

        if self.config.gui_interaction.enabled and main_computer_interactive:
            parts.extend(self._build_gui_interaction())

        if self.config.time.enabled:
            parts.extend(self._build_time())

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

    def _build_time(self) -> list[str]:
        """Build the Current Date & Time section.

        Mirrors OpenClaw's system prompt which injects the current UTC timestamp
        so the agent knows the date/time without needing a tool call.
        """
        now = datetime.now(timezone.utc)
        return [
            "## Current Date & Time",
            "",
            "- **Time zone:** UTC",
            f"- **Current:** {now.strftime('%Y-%m-%d %H:%M UTC')}",
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
                "observations, or task state: run memory_search on TASK_MEMORY.md + "
                "memory/session-*.md; then use memory_get to pull only the needed lines. "
                "If low confidence after search, say you checked."
            ),
            "Citations: include Source: <path#line> when referencing memory snippets.",
            "",
        ]

    def _build_delegation(self, tool_summaries: dict[str, str]) -> list[str]:
        """Build the Delegation section.

        Emitted when any of delegate_general / delegate_gui / subagents are
        registered. Iterates the present subset and emits only the relevant
        subsections. Mirrors OpenClaw's absence-is-the-signal pattern: absent
        tools aren't described and aren't mentioned as "disabled."

        Migrated from the static openclaw/AGENTS.md Delegation section so the
        prose tracks the actual tool list rather than drifting when flags
        change (``disable_delegate_gui`` etc.).
        """
        has_general = "delegate_general" in tool_summaries
        has_gui = "delegate_gui" in tool_summaries
        has_subagents = "subagents" in tool_summaries
        if not (has_general or has_gui):
            return []

        lines: list[str] = [
            "## Delegation",
            "",
            (
                "You can delegate focused work to subagents when it helps — "
                "e.g. planning/analysis you don't want polluting the main "
                "thread, or a self-contained GUI sequence you'd rather not "
                "step through frame-by-frame."
            ),
            "",
        ]
        if has_general:
            lines.extend(
                [
                    "### `delegate_general(task, ...)` — async, auto-announces",
                    "",
                    (
                        "Spawns a general-purpose subagent session that has "
                        "**no VM access** — only memory tools and LLM "
                        "reasoning. Use for: synthesizing plans from what "
                        "you've observed; analyzing tricky text/content in "
                        "memory; deciding between multiple strategies. "
                        "Returns immediately with `{status: accepted, "
                        "run_id, note}`. Keep working — **do NOT poll**. "
                        "When the subagent finishes, its result is injected "
                        "automatically as a `[Subagent Result]` user message "
                        "on a later turn. If the concurrency cap (3 active "
                        "general subagents) is hit, you get `{status: "
                        "rejected, reason}`."
                    ),
                    "",
                ]
            )
        if has_gui:
            lines.extend(
                [
                    "### `delegate_gui(instruction, ...)` — async, auto-announces",
                    "",
                    (
                        "Spawns a GUI automation subagent driven by a vision "
                        "model. It takes over the VM for a bounded number of "
                        "steps (default 15) to perform a focused GUI "
                        "sequence — open an app, fill a form, click through "
                        "a wizard. Returns immediately with `{status: "
                        "accepted, run_id, note}`. Keep working on non-VM "
                        "tasks — **do NOT poll**. When the subagent "
                        "finishes, its result is injected as a `[Subagent "
                        "Result]` user message followed by a fresh VM "
                        "screenshot on a later turn. While the GUI subagent "
                        "is running, the VM is occupied — **do not call "
                        "`delegate_gui` again or use `computer` directly "
                        "until it completes**."
                    ),
                    "",
                ]
            )
        if has_subagents:
            steer_scope = "general or GUI" if has_gui else "general"
            lines.extend(
                [
                    "### `subagents(action=list | kill | steer, target=..., message=...)` — observability + control",
                    "",
                    (
                        "- `action=list` returns active (running/pending) and "
                        "recent (terminal) runs. **Do NOT poll** during "
                        "normal operation — results auto-announce. Use "
                        "`list` only if you suspect something is stuck."
                    ),
                    (
                        "- `action=kill` (with `target=<run_id>`) cancels a "
                        "runaway general subagent. The subagent transitions "
                        "to `killed` and no completion message will be "
                        "announced for that run."
                    ),
                    (
                        f"- `action=steer` (with `target` and `message`) "
                        f"sends a follow-up message into a **running "
                        f"subagent** ({steer_scope}) to refine or redirect "
                        f"its work mid-flight. The message is injected "
                        f"between the subagent's own turns. Target can be a "
                        f"run_id, label, run_id prefix, or `\"last\"`. Max "
                        f"4000 chars."
                    ),
                    "",
                ]
            )
        lines.extend(
            [
                "### Rules of thumb",
                "",
                "- Don't delegate trivial things you can do in a single tool call.",
                (
                    "- Don't spawn a general subagent and then sit idle "
                    "waiting — keep making forward progress and the result "
                    "will arrive when it arrives."
                ),
                "- Don't nest delegation: subagents can't spawn further subagents.",
                "",
            ]
        )
        return lines

    def _build_gui_interaction(self) -> list[str]:
        """Build the GUI Interaction section.

        Emitted only when the main ``computer`` tool can perform interactive
        actions. Migrated from the static openclaw/AGENTS.md General Behavior
        bullets so the guidance doesn't appear when the main computer is
        restricted (``disable_main_computer=True``).
        """
        return [
            "## GUI Interaction",
            "",
            (
                "- Be precise with mouse clicks — target the center of "
                "buttons and UI elements."
            ),
            (
                "- Use keyboard shortcuts when they are more reliable than "
                "mouse clicks."
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
            lines.append(f"### {cf.path}")
            lines.append("```")
            lines.append(cf.content)
            lines.append("```")
            lines.append("")
        return lines
