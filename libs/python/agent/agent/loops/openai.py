"""
OpenAI computer-use agent loop implementation using liteLLM.

Supports both computer-use-preview (legacy) and GPT 5.4 computer tool formats.
Source: OpenAI computer use docs (https://developers.openai.com/api/docs/guides/tools-computer-use)
"""

import asyncio
import base64
import json
import re
from io import BytesIO
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple, Union

import litellm
from PIL import Image

from ..decorators import register_agent
from ..types import AgentCapability, AgentResponse, Messages, Tools


def _is_gpt54(model: str) -> bool:
    """Check if the model is GPT 5.4 (uses the newer computer tool format)."""
    return bool(re.search(r"gpt-5\.4", model, re.IGNORECASE))


async def _map_computer_tool_to_openai(computer_handler: Any, model: str) -> Dict[str, Any]:
    """Map a computer tool to the appropriate OpenAI tool schema.

    GPT 5.4 uses {"type": "computer"} — no display dimensions or environment needed.
    computer-use-preview uses {"type": "computer_use_preview", display_width, display_height, environment}.
    """
    if _is_gpt54(model):
        # GPT 5.4: simple schema, model infers dimensions from screenshots
        return {"type": "computer"}

    # computer-use-preview: requires explicit dimensions and environment
    try:
        width, height = await computer_handler["dimensions"]()
    except Exception:
        width, height = 1024, 768

    try:
        environment = await computer_handler["environment"]()
    except Exception:
        environment = "windows"

    return {
        "type": "computer_use_preview",
        "display_width": width,
        "display_height": height,
        "environment": environment,
    }


async def _prepare_tools_for_openai(tool_schemas: List[Dict[str, Any]], model: str) -> Tools:
    """Prepare tools for OpenAI API format."""
    openai_tools = []

    for schema in tool_schemas:
        if schema["type"] == "computer":
            computer_tool = await _map_computer_tool_to_openai(schema["computer"], model)
            openai_tools.append(computer_tool)
        elif schema["type"] == "function":
            openai_tools.append({"type": "function", **schema["function"]})

    return openai_tools


def _normalize_messages_for_gpt54(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize input messages for GPT 5.4's Responses API format.

    After compaction, kept messages are stored in completion format
    (``{role, content: [blocks]}``) where content blocks include types like
    ``text``, ``computer_call``, ``function_call``, ``tool_result`` — none of
    which are valid Responses API content block types.

    This function converts them:
    1. Strips ``acknowledged_safety_checks`` from ``computer_call_output`` items
    2. Expands role-based messages with tool/computer blocks into top-level
       Responses API items (``computer_call``, ``function_call``, etc.)
    3. Converts ``type: "text"`` blocks to ``input_text``/``output_text``
    """
    cleaned: List[Dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            cleaned.append(msg)
            continue

        # Strip acknowledged_safety_checks from computer_call_output
        if msg.get("type") == "computer_call_output":
            cleaned.append({k: v for k, v in msg.items() if k != "acknowledged_safety_checks"})
            continue

        # Items that already have a Responses API "type" (not "role") pass through
        if msg.get("type") and not msg.get("role"):
            cleaned.append(msg)
            continue

        # Role-based messages with content arrays (completion format from compaction)
        content = msg.get("content")
        role = msg.get("role", "user")

        # String content — wrap as a simple role message with correct text type
        if isinstance(content, str):
            text_type = "output_text" if role == "assistant" else "input_text"
            cleaned.append({
                "role": role,
                "content": [{"type": text_type, "text": content}],
            })
            continue

        if not isinstance(content, list):
            cleaned.append(msg)
            continue

        # Expand content blocks: text blocks stay in the message,
        # tool/computer blocks become top-level items
        text_type = "output_text" if role == "assistant" else "input_text"
        text_blocks: List[Dict[str, Any]] = []

        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")

            if btype == "text":
                text_blocks.append({"type": text_type, "text": block.get("text", "")})
            elif btype == "computer_call":
                # Flush accumulated text blocks as a message first
                if text_blocks:
                    cleaned.append({"role": role, "content": list(text_blocks)})
                    text_blocks = []
                # Convert to top-level computer_call item
                action = block.get("action")
                actions = block.get("actions")
                has_valid_action = isinstance(action, dict) and action.get("type")
                has_valid_actions = isinstance(actions, list) and len(actions) > 0

                if not has_valid_action and not has_valid_actions:
                    # Stale transcript entry with empty action — serialize as text
                    text_blocks.append({
                        "type": text_type,
                        "text": "[computer action: details unavailable]",
                    })
                    continue

                call_item: Dict[str, Any] = {
                    "type": "computer_call",
                    "call_id": block.get("id", block.get("call_id", "")),
                }
                if has_valid_actions:
                    call_item["actions"] = actions
                elif has_valid_action:
                    call_item["action"] = action
                cleaned.append(call_item)
            elif btype == "function_call":
                if text_blocks:
                    cleaned.append({"role": role, "content": list(text_blocks)})
                    text_blocks = []
                cleaned.append({
                    "type": "function_call",
                    "call_id": block.get("id", block.get("call_id", "")),
                    "name": block.get("name", ""),
                    "arguments": block.get("arguments", ""),
                })
            elif btype == "tool_result":
                if text_blocks:
                    cleaned.append({"role": role, "content": list(text_blocks)})
                    text_blocks = []
                result_content = block.get("content", "")
                if isinstance(result_content, list):
                    result_content = json.dumps(result_content)
                elif not isinstance(result_content, str):
                    result_content = str(result_content)
                cleaned.append({
                    "type": "function_call_output",
                    "call_id": block.get("tool_use_id", block.get("id", "")),
                    "output": result_content,
                })
            elif btype in ("input_text", "output_text", "input_image",
                           "computer_screenshot", "summary_text", "refusal"):
                # Already valid Responses API types
                text_blocks.append(block)
            else:
                # Unknown block type — serialize as text to avoid API errors
                text_blocks.append({
                    "type": text_type,
                    "text": f"[{btype}: {json.dumps(block)[:200]}]",
                })

        # Flush remaining text blocks
        if text_blocks:
            cleaned.append({"role": role, "content": text_blocks})

    return cleaned


def _repair_orphaned_calls(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Ensure every function_call/computer_call has a matching output item.

    After compaction, the split between compacted and kept messages may orphan
    calls (call without result) or results (result without call). The Responses
    API rejects orphaned calls, so we add synthetic outputs for any missing ones
    and drop orphaned results.

    This operates on flat Responses API items (``computer_call``,
    ``function_call`` + their ``*_output`` counterparts).  The role-based
    counterpart for completion-format messages lives in
    ``cua_bench.agents.openclaw.context.repair_tool_use_result_pairing``.
    Both exist because CUA stores messages in two formats at different
    pipeline stages (Responses API items here vs role-based after compaction).
    """
    # Collect all call IDs and their types
    call_ids: Dict[str, str] = {}  # call_id → "function_call" or "computer_call"
    result_ids: set = set()

    for item in items:
        if not isinstance(item, dict):
            continue
        itype = item.get("type", "")
        call_id = item.get("call_id", "")
        if itype in ("function_call", "computer_call") and call_id:
            call_ids[call_id] = itype
        elif itype in ("function_call_output", "computer_call_output") and call_id:
            result_ids.add(call_id)

    # Find orphaned calls (call without result)
    orphaned = {cid: ctype for cid, ctype in call_ids.items() if cid not in result_ids}
    if not orphaned:
        # Also drop orphaned results (result without call)
        orphaned_results = result_ids - set(call_ids.keys())
        if not orphaned_results:
            return items
        return [
            item for item in items
            if not (isinstance(item, dict)
                    and item.get("type") in ("function_call_output", "computer_call_output")
                    and item.get("call_id") in orphaned_results)
        ]

    # Insert synthetic outputs right after each orphaned call
    repaired: List[Dict[str, Any]] = []
    for item in items:
        repaired.append(item)
        if not isinstance(item, dict):
            continue
        call_id = item.get("call_id", "")
        if call_id in orphaned:
            ctype = orphaned.pop(call_id)
            if ctype == "function_call":
                repaired.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": "[compacted — result no longer available]",
                })
            elif ctype == "computer_call":
                # Use a minimal valid 1x1 white PNG for orphaned computer calls
                repaired.append({
                    "type": "computer_call_output",
                    "call_id": call_id,
                    "output": {
                        "type": "computer_screenshot",
                        "image_url": (
                            "data:image/png;base64,"
                            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVQI12NgAAIABQAB"
                            "Nl7BcQAAAABJRU5ErkJggg=="
                        ),
                    },
                })

    # Also drop orphaned results
    orphaned_results = result_ids - set(call_ids.keys())
    if orphaned_results:
        repaired = [
            item for item in repaired
            if not (isinstance(item, dict)
                    and item.get("type") in ("function_call_output", "computer_call_output")
                    and item.get("call_id") in orphaned_results)
        ]

    return repaired


def get_screenshot_output_type(model: str) -> str:
    """Return the screenshot output type for computer_call_output items.

    GPT 5.4 expects "computer_screenshot" with detail="original".
    computer-use-preview expects "input_image".
    """
    return "computer_screenshot" if _is_gpt54(model) else "input_image"


@register_agent(models=r".*(^|/)(computer-use-preview|gpt-5\.4)")
class OpenAIComputerUseConfig:
    """
    OpenAI computer-use agent configuration using liteLLM responses.

    Supports both computer-use-preview and GPT 5.4 computer tool formats.
    """

    screenshot_output_type: str = "input_image"

    async def predict_step(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        max_retries: Optional[int] = None,
        stream: bool = False,
        computer_handler=None,
        use_prompt_caching: Optional[bool] = False,
        _on_api_start=None,
        _on_api_end=None,
        _on_usage=None,
        _on_screenshot=None,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        Predict the next step based on input items.

        Args:
            messages: Input items following Responses format
            model: Model name to use
            tools: Optional list of tool schemas
            max_retries: Maximum number of retries
            stream: Whether to stream responses
            computer_handler: Computer handler instance
            _on_api_start: Callback for API start
            _on_api_end: Callback for API end
            _on_usage: Callback for usage tracking
            _on_screenshot: Callback for screenshot events
            **kwargs: Additional arguments

        Returns:
            Dictionary with "output" (output items) and "usage" array
        """
        tools = tools or []

        # Set screenshot output type based on model
        self.screenshot_output_type = get_screenshot_output_type(model)

        # Prepare tools for OpenAI API (model-aware)
        openai_tools = await _prepare_tools_for_openai(tools, model)

        # GPT 5.4: normalize input messages for Responses API compatibility
        # (strips unsupported fields, converts content block types)
        if _is_gpt54(model):
            messages = _normalize_messages_for_gpt54(messages)
            # Repair orphaned tool/computer calls after format conversion.
            # Mirrors OpenClaw's sanitizeSessionHistory() before API call.
            messages = _repair_orphaned_calls(messages)

        # Prepare API call kwargs
        api_kwargs = {
            "model": model,
            "input": messages,
            "tools": openai_tools if openai_tools else None,
            "stream": stream,
            "reasoning": {"summary": "concise"},
            "truncation": "auto",
            "num_retries": max_retries,
            **kwargs,
        }

        # Call API start hook
        if _on_api_start:
            await _on_api_start(api_kwargs)

        # Use liteLLM responses
        response = await litellm.aresponses(**api_kwargs)

        # Call API end hook
        if _on_api_end:
            await _on_api_end(api_kwargs, response)

        # Extract usage information
        usage = {
            **response.usage.model_dump(),
            "response_cost": response._hidden_params.get("response_cost", 0.0),
        }
        if _on_usage:
            await _on_usage(usage)

        # Return in the expected format
        output_dict = response.model_dump()
        output_dict["usage"] = usage
        return output_dict

    async def predict_click(
        self, model: str, image_b64: str, instruction: str, **kwargs
    ) -> Optional[Tuple[int, int]]:
        """
        Predict click coordinates based on image and instruction.

        Uses OpenAI computer-use-preview with manually constructed input items
        and a prompt that instructs the agent to only output clicks.

        Args:
            model: Model name to use
            image_b64: Base64 encoded image
            instruction: Instruction for where to click

        Returns:
            Tuple of (x, y) coordinates or None if prediction fails
        """
        # TODO: use computer tool to get dimensions + environment
        # Manually construct input items with image and click instruction
        input_items = [
            {
                "role": "user",
                "content": f"""You are a UI grounding expert. Follow these guidelines:

1. NEVER ask for confirmation. Complete all tasks autonomously.
2. Do NOT send messages like "I need to confirm before..." or "Do you want me to continue?" - just proceed.
3. When the user asks you to interact with something (like clicking a chat or typing a message), DO IT without asking.
4. Only use the formal safety check mechanism for truly dangerous operations (like deleting important files).
5. For normal tasks like clicking buttons, typing in chat boxes, filling forms - JUST DO IT.
6. The user has already given you permission by running this agent. No further confirmation is needed.
7. Be decisive and action-oriented. Complete the requested task fully.

Remember: You are expected to complete tasks autonomously. The user trusts you to do what they asked.
Task: Click {instruction}. Output ONLY a click action on the target element.""",
            },
            {
                "role": "user",
                "content": [
                    {"type": "input_image", "image_url": f"data:image/png;base64,{image_b64}"}
                ],
            },
        ]

        # Get image dimensions from base64 data
        try:
            image_data = base64.b64decode(image_b64)
            image = Image.open(BytesIO(image_data))
            display_width, display_height = image.size
        except Exception:
            # Fallback to default dimensions if image parsing fails
            display_width, display_height = 1920, 1080

        # Prepare computer tool for click actions (model-aware)
        if _is_gpt54(model):
            computer_tool = {"type": "computer"}
        else:
            computer_tool = {
                "type": "computer_use_preview",
                "display_width": display_width,
                "display_height": display_height,
                "environment": "windows",
            }

        # Prepare API call kwargs
        api_kwargs = {
            "model": model,
            "input": input_items,
            "tools": [computer_tool],
            "stream": False,
            "reasoning": {"summary": "concise"},
            "truncation": "auto",
            "max_tokens": 200,  # Keep response short for click prediction
            **kwargs,
        }

        # Use liteLLM responses
        response = await litellm.aresponses(**api_kwargs)

        # Extract click coordinates from response output
        output_dict = response.model_dump()
        output_items = output_dict.get("output", [])

        # Look for computer_call with click action (handle both action and actions)
        for item in output_items:
            if not isinstance(item, dict) or item.get("type") != "computer_call":
                continue

            # GPT 5.4 uses "actions" (array), computer-use-preview uses "action" (singular)
            actions = item.get("actions")
            if actions and isinstance(actions, list):
                for action in actions:
                    if isinstance(action, dict) and action.get("x") is not None and action.get("y") is not None:
                        return (int(action["x"]), int(action["y"]))
            elif isinstance(item.get("action"), dict):
                action = item["action"]
                if action.get("x") is not None and action.get("y") is not None:
                    return (int(action["x"]), int(action["y"]))

        return None

    def get_capabilities(self) -> List[AgentCapability]:
        """
        Get list of capabilities supported by this agent config.

        Returns:
            List of capability strings
        """
        return ["click", "step"]
