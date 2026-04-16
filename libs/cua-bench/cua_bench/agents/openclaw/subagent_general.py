"""General subagent — async one-shot worker with multi-turn function-calling.

A lightweight litellm.acompletion() loop that inherits analysis/memory tools
(but NOT Computer or delegation tools), runs inline tool execution, and reports
results back via the SubagentRegistry completion queue.

This is NOT a full OpenClawComputerAgent — no VM access, no trajectory, no
compaction, no session persistence. The asyncio.Task wrapper lives in the
delegation tool (US-SUB-005); this module is the pure async function.

Design adapted from OpenClaw's subagent architecture:
  - subagent-announce.ts:buildSubagentSystemPrompt (role, constraints, output format)
  - subagent-spawn.ts (spawn flow, registry lifecycle)
  - Simplified for CUA's single-process asyncio model (no gateway sessions)
"""

from __future__ import annotations

import json as _json
from typing import Any

from agent.model_config import resolve_model
from agent.tools.base import BaseTool

from .subagent_registry import SubagentRegistry, SubagentUsage

# Tools the general subagent CAN use.
ALLOWED_TOOL_NAMES = frozenset({
    "analyze_image",
    "memory_search",
    "memory_get",
    "memory_write",
})

# Tools explicitly excluded (even if passed in).
EXCLUDED_TOOL_NAMES = frozenset({
    "computer",
    "milestone",
    "delegate_general",
    "delegate_gui",
    "subagents",
})

DEFAULT_MAX_STEPS = 5


def _build_subagent_system_prompt(task: str) -> str:
    """Build a focused worker system prompt adapted from OpenClaw's buildSubagentSystemPrompt.

    Reference: openclaw/src/agents/subagent-announce.ts:47-104
    """
    return "\n".join([
        "# Subagent Context",
        "",
        "You are a **focused worker subagent** spawned by the main agent for a specific task.",
        "",
        "## Your Task",
        f"- {task}",
        "",
        "## Rules",
        "1. **Stay focused** - Do your assigned task, nothing else",
        "2. **Complete and return** - Your final message is automatically reported to the main agent",
        "3. **Don't initiate** - No heartbeats, no proactive actions, no side quests",
        "4. **No computer actions** - You cannot interact with the desktop; use only the tools provided",
        "5. **Be ephemeral** - You may be terminated after task completion. That's fine.",
        "",
        "## Output Format",
        "When complete, respond with:",
        "- What you accomplished or found",
        "- Any relevant details the main agent should know",
        "- Keep it concise but informative",
        "",
        "## What You DON'T Do",
        "- NO user conversations (that's the main agent's job)",
        "- NO computer/mouse/keyboard actions",
        "- NO external messages",
        "- NO pretending to be the main agent",
    ])


def _filter_tools(tools: list) -> list[BaseTool]:
    """Filter tools to the subset allowed for general subagents.

    Includes only BaseTool instances whose name is in ALLOWED_TOOL_NAMES.
    Excludes anything in EXCLUDED_TOOL_NAMES or that isn't a BaseTool.
    """
    filtered: list[BaseTool] = []
    for tool in tools:
        if not isinstance(tool, BaseTool):
            continue
        name = getattr(tool, "name", None)
        if name is None:
            continue
        if name in EXCLUDED_TOOL_NAMES:
            continue
        if name in ALLOWED_TOOL_NAMES:
            filtered.append(tool)
    return filtered


def _tools_to_litellm_schema(tools: list[BaseTool]) -> list[dict[str, Any]]:
    """Convert BaseTool instances to litellm function-calling format."""
    return [{"type": "function", "function": tool.function} for tool in tools]


async def run_general_subagent(
    *,
    task: str,
    model: str,
    tools: list,
    registry: SubagentRegistry,
    run_id: str,
    max_steps: int = DEFAULT_MAX_STEPS,
    thinking_params: dict[str, Any] | None = None,
) -> None:
    """Run a general subagent as a multi-turn function-calling loop.

    Args:
        task: The task description for the subagent.
        model: Model string (e.g. "anthropic/claude-sonnet-4-20250514").
        tools: Full tool list from build_tools() — will be filtered.
        registry: SubagentRegistry for lifecycle reporting.
        run_id: Run ID from registry.register().
        max_steps: Maximum number of LLM call iterations (default 5).
        thinking_params: Optional provider-specific thinking kwargs.
    """
    import litellm

    usage = SubagentUsage()

    try:
        registry.mark_running(run_id)

        resolved = resolve_model(model)
        filtered_tools = _filter_tools(tools)
        tool_schemas = _tools_to_litellm_schema(filtered_tools)
        tool_map = {t.name: t for t in filtered_tools}

        system_prompt = _build_subagent_system_prompt(task)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task},
        ]

        result_text = ""

        for _step in range(max_steps):
            kwargs: dict[str, Any] = {
                "model": resolved.model,
                "messages": messages,
                "max_tokens": 4096,
                "temperature": 1.0,
                **(thinking_params or {}),
            }
            if tool_schemas:
                kwargs["tools"] = tool_schemas

            response = await litellm.acompletion(**kwargs)
            choice = response.choices[0]

            # Accumulate token usage.
            resp_usage = getattr(response, "usage", None)
            if resp_usage is not None:
                usage.input_tokens += getattr(resp_usage, "prompt_tokens", 0)
                usage.output_tokens += getattr(resp_usage, "completion_tokens", 0)

            assistant_content = choice.message.content or ""
            tool_calls = choice.message.tool_calls

            if not tool_calls:
                # Final answer — no more tool calls.
                result_text = assistant_content.strip()
                break

            # Append assistant message with tool_calls for multi-turn.
            assistant_msg: dict[str, Any] = {"role": "assistant", "content": assistant_content}
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in tool_calls
            ]
            messages.append(assistant_msg)

            # Execute each tool call inline.
            for tc in tool_calls:
                tool_name = tc.function.name
                tool_args = tc.function.arguments
                tool = tool_map.get(tool_name)

                if tool is None:
                    tool_result = f"Error: tool '{tool_name}' is not available to this subagent."
                else:
                    try:
                        tool_result = tool.call(tool_args)
                        if not isinstance(tool_result, str):
                            tool_result = _json.dumps(tool_result)
                    except Exception as e:
                        tool_result = f"Error executing {tool_name}: {e}"

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": str(tool_result),
                })

            # After tool execution, the last assistant text becomes partial result.
            result_text = assistant_content.strip()
        else:
            # Loop exhausted max_steps without a final text-only response.
            if not result_text:
                result_text = "(subagent reached max steps without a final response)"

        print(f"[Subagent] General subagent {run_id} completed ({usage.input_tokens}+{usage.output_tokens} tokens)")
        registry.complete(run_id, result_text, usage)

    except Exception as e:
        print(f"[Subagent] General subagent {run_id} failed: {e}")
        registry.fail(run_id, str(e), usage)
