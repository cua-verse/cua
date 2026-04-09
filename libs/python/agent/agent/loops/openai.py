"""
OpenAI computer-use agent loop implementation using liteLLM.

Supports both computer-use-preview (legacy) and GPT 5.4 computer tool formats.
Source: OpenAI computer use docs (https://developers.openai.com/api/docs/guides/tools-computer-use)
"""

import asyncio
import base64
import json
from io import BytesIO
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple, Union

import litellm
from PIL import Image

from ..decorators import register_agent
from ..model_config import get_model_config
from ..types import AgentCapability, AgentResponse, Messages, Tools


async def _map_computer_tool_to_openai(computer_handler: Any, model: str) -> Dict[str, Any]:
    """Map a computer tool to the appropriate OpenAI tool schema.

    Uses ModelConfig.tool_schema_type to determine the correct schema.
    GPT 5.4 uses {"type": "computer"} — no display dimensions or environment needed.
    computer-use-preview uses {"type": "computer_use_preview", display_width, display_height, environment}.
    """
    config = get_model_config(model)
    if config.tool_schema_type == "computer":
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
        "type": config.tool_schema_type,
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

        # Set screenshot output type based on model config registry
        self.screenshot_output_type = get_model_config(model).screenshot_output_type

        # Prepare tools for OpenAI API (model-aware)
        openai_tools = await _prepare_tools_for_openai(tools, model)

        # Message normalization and orphan repair is now handled by the
        # sanitize_items() pipeline in agent_loop.py (US-OC-039), which runs
        # before predict_step(). No per-model normalization needed here.

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

        # Prepare computer tool for click actions (model-aware via config registry)
        click_config = get_model_config(model)
        if click_config.tool_schema_type == "computer":
            computer_tool = {"type": "computer"}
        else:
            computer_tool = {
                "type": click_config.tool_schema_type,
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
