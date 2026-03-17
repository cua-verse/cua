"""Transcript helpers — group CUA step output into assistant/tool content blocks.

Moved from openclaw_agent.py (US-OC-028) to break a cross-package import
(agent_loop.py was importing from ..openclaw_agent).

Reference:
  - openclaw_agent.py — original location of these functions
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _find_latest_screenshot(trajectory_dir: Path | None) -> str:
    """Find the most recently saved screenshot_after.png in trajectory_dir.

    TrajectorySaverCallback saves one *_screenshot_after.png per computer action
    into trajectories/<trajectory_id>/turn_NNN/. The newest file corresponds to
    the action just completed.

    Returns the absolute path string, or "image:trajectory" if not found.
    """
    if not trajectory_dir or not trajectory_dir.exists():
        return "image:trajectory"
    screenshots = list(trajectory_dir.rglob("*_screenshot_after.png"))
    if not screenshots:
        return "image:trajectory"
    return str(max(screenshots, key=lambda p: p.stat().st_mtime))


def group_step_output(
    output_items: list[dict[str, Any]],
    trajectory_dir: Path | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Group a step's output items into assistant content blocks and tool results.

    CUA SDK yields multiple output items per step (text, function_call,
    computer_call, their outputs). This function batches them into two lists:
    - assistant_content: text + function_call + computer_call blocks (one assistant turn)
    - tool_results: function_call_output + computer_call_output blocks (one tool turn)

    Args:
        output_items: The result["output"] list from a CUA agent step.
        trajectory_dir: Path to trajectory directory for screenshot resolution.

    Returns:
        (assistant_content, tool_results) tuple of content block lists.
    """
    assistant_content: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []

    for item in output_items:
        item_type = item.get("type")
        if item_type == "message":
            for block in item.get("content", []):
                if block.get("text"):
                    assistant_content.append({"type": "text", "text": block["text"]})
        elif item_type == "function_call":
            assistant_content.append({
                "type": "function_call",
                "id": item.get("call_id", ""),
                "name": item.get("name", ""),
                "arguments": item.get("arguments", ""),
            })
        elif item_type == "computer_call":
            assistant_content.append({
                "type": "computer_call",
                "id": item.get("call_id", ""),
                "action": item.get("action", {}),
            })
        elif item_type == "function_call_output":
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": item.get("call_id", ""),
                "content": item.get("output", ""),
            })
        elif item_type == "computer_call_output":
            output = item.get("output", {})
            call_id = item.get("call_id", "")
            if isinstance(output, dict) and output.get("type") == "input_image":
                content_str = _find_latest_screenshot(trajectory_dir)
            else:
                content_str = str(output)[:500]
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": call_id,
                "content": content_str,
            })

    return assistant_content, tool_results
