"""Unit tests for ComputerAgent class.

This file tests ONLY the ComputerAgent initialization and basic functionality.
Following SRP: This file tests ONE class (ComputerAgent).
All external dependencies (liteLLM, Computer) are mocked.
"""

from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest


class TestComputerAgentInitialization:
    """Test ComputerAgent initialization (SRP: Only tests initialization)."""

    @patch("agent.agent.litellm")
    def test_agent_initialization_with_model(self, mock_litellm, disable_telemetry):
        """Test that agent can be initialized with a model string."""
        from agent import ComputerAgent

        agent = ComputerAgent(model="anthropic/claude-sonnet-4-5-20250929")

        assert agent is not None
        assert hasattr(agent, "model")
        assert agent.model == "anthropic/claude-sonnet-4-5-20250929"

    @patch("agent.agent.litellm")
    def test_agent_initialization_with_tools(self, mock_litellm, disable_telemetry, mock_computer):
        """Test that agent can be initialized with tools."""
        from agent import ComputerAgent

        agent = ComputerAgent(model="anthropic/claude-sonnet-4-5-20250929", tools=[mock_computer])

        assert agent is not None
        assert hasattr(agent, "tools")

    @patch("agent.agent.litellm")
    def test_agent_initialization_with_max_budget(self, mock_litellm, disable_telemetry):
        """Test that agent can be initialized with max trajectory budget."""
        from agent import ComputerAgent

        budget = 5.0
        agent = ComputerAgent(
            model="anthropic/claude-sonnet-4-5-20250929", max_trajectory_budget=budget
        )

        assert agent is not None

    @patch("agent.agent.litellm")
    def test_agent_requires_model(self, mock_litellm, disable_telemetry):
        """Test that agent requires a model parameter."""
        from agent import ComputerAgent

        with pytest.raises(TypeError):
            # Should fail without model parameter - intentionally missing required argument
            ComputerAgent()  # type: ignore[call-arg]


class TestComputerAgentRun:
    """Test ComputerAgent.run() method (SRP: Only tests run logic)."""

    @pytest.mark.asyncio
    @patch("agent.agent.litellm")
    async def test_agent_run_with_messages(self, mock_litellm, disable_telemetry, sample_messages):
        """Test that agent.run() works with valid messages."""
        from agent import ComputerAgent

        # Mock liteLLM response
        mock_response = {
            "id": "chatcmpl-test",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Test response"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }

        mock_litellm.acompletion = AsyncMock(return_value=mock_response)

        agent = ComputerAgent(model="anthropic/claude-sonnet-4-5-20250929")

        # Run should return an async generator
        result_generator = agent.run(sample_messages)

        assert result_generator is not None
        # Check it's an async generator
        assert hasattr(result_generator, "__anext__")

    def test_agent_has_run_method(self, disable_telemetry):
        """Test that agent has run method available."""
        from agent import ComputerAgent

        agent = ComputerAgent(model="anthropic/claude-sonnet-4-5-20250929")

        # Verify run method exists
        assert hasattr(agent, "run")
        assert callable(agent.run)

    def test_agent_has_agent_loop(self, disable_telemetry):
        """Test that agent has agent_loop initialized."""
        from agent import ComputerAgent

        agent = ComputerAgent(model="anthropic/claude-sonnet-4-5-20250929")

        # Verify agent_loop is initialized
        assert hasattr(agent, "agent_loop")
        assert agent.agent_loop is not None


class TestComputerAgentTypes:
    """Test AgentResponse and Messages types (SRP: Only tests type definitions)."""

    def test_messages_type_exists(self):
        """Test that Messages type is exported."""
        from agent import Messages

        assert Messages is not None

    def test_agent_response_type_exists(self):
        """Test that AgentResponse type is exported."""
        from agent import AgentResponse

        assert AgentResponse is not None


class TestComputerAgentIntegration:
    """Test ComputerAgent integration with Computer tool (SRP: Integration within package)."""

    def test_agent_accepts_computer_tool(self, disable_telemetry, mock_computer):
        """Test that agent can be initialized with Computer tool."""
        from agent import ComputerAgent

        agent = ComputerAgent(model="anthropic/claude-sonnet-4-5-20250929", tools=[mock_computer])

        # Verify agent accepted the tool
        assert agent is not None
        assert hasattr(agent, "tools")


class TestHandleFunctionCallScreenshot:
    """Regression tests for the function_call path of action="screenshot".

    The computer dispatch at agent.py's `_handle_item` previously returned the
    raw base64 PNG as the `function_call_output.output` content, ballooning the
    transcript by ~58K tokens per explicit screenshot call and escaping
    `only_n_most_recent_images` pruning (which only operates on `image_url`
    blocks, not tool-result text). The fix: for action="screenshot", return
    the standard success sentinel in the function output; the base64 still
    flows to the model via the adjacent `user` message with an `input_image`
    content block, matching click/keypress/etc.
    """

    def test_screenshot_returns_sentinel_in_output(
        self, disable_telemetry, mock_computer
    ):
        import asyncio
        import json as _json

        from agent import ComputerAgent

        # Screenshot returns a base64 string (this is the real CUA Computer
        # behavior at cua.py:39-47). The mock returns a long base64-lookalike
        # so a regression would show up as "sees this payload in output".
        long_b64 = "iVBORw0KGgo" + "A" * 1000
        mock_computer.screenshot = AsyncMock(return_value=long_b64)

        agent = ComputerAgent(
            model="anthropic/claude-sonnet-4-5-20250929",
            tools=[mock_computer],
        )

        item = {
            "type": "function_call",
            "name": "computer",
            "call_id": "call_test_screenshot",
            "arguments": '{"action": "screenshot"}',
        }
        result = asyncio.run(agent._handle_item(item, mock_computer))

        # _handle_item returns [function_call_output, image_message].
        assert len(result) == 2
        call_output, image_message = result

        assert call_output["type"] == "function_call_output"
        assert call_output["call_id"] == "call_test_screenshot"
        output_str = call_output["output"]
        # Sentinel — NOT a base64 PNG string.
        output_json = _json.loads(output_str)
        assert output_json == {"success": True, "screenshot_captured": True}
        # Belt and suspenders: make sure the base64 didn't sneak in anywhere.
        assert "iVBORw0KGgo" not in output_str

        # The adjacent user message still carries the screenshot for the model.
        assert image_message["role"] == "user"
        assert image_message["content"][0]["type"] == "input_image"
        assert long_b64 in image_message["content"][0]["image_url"]

    def test_non_screenshot_action_preserves_result_dict(
        self, disable_telemetry, mock_computer
    ):
        """Regression guard: click/keypress/etc. must still surface their real
        action_result in the tool output so real failures aren't masked."""
        import asyncio
        import json as _json

        from agent import ComputerAgent

        mock_computer.screenshot = AsyncMock(return_value="base64data")
        mock_computer.click = AsyncMock(
            return_value={"clicked": True, "target_x": 100, "target_y": 200}
        )

        agent = ComputerAgent(
            model="anthropic/claude-sonnet-4-5-20250929",
            tools=[mock_computer],
        )
        item = {
            "type": "function_call",
            "name": "computer",
            "call_id": "call_test_click",
            "arguments": '{"action": "click", "x": 100, "y": 200, "button": "left"}',
        }
        result = asyncio.run(agent._handle_item(item, mock_computer))
        call_output = result[0]

        payload = _json.loads(call_output["output"])
        assert payload == {"clicked": True, "target_x": 100, "target_y": 200}


class TestNormalizeKey:
    """Case-insensitive arrow-key aliasing in cuaComputerHandler._normalize_key.

    Regression guard for the uppercase-only dict probe that silently passed
    ``arrowup`` / ``ArrowUp`` through to the VM raw (pynput has no such key
    name), so only the literal ``ARROWUP`` spelling moved the cursor.
    """

    @pytest.mark.parametrize(
        "inp,expected",
        [
            # Arrow aliases — every casing must resolve to the canonical form.
            ("ARROWUP", "up"),
            ("arrowup", "up"),
            ("ArrowUp", "up"),
            ("ARROWDOWN", "down"),
            ("arrowdown", "down"),
            ("ArrowDown", "down"),
            ("ARROWLEFT", "left"),
            ("arrowleft", "left"),
            ("ARROWRIGHT", "right"),
            ("arrowright", "right"),
            # Canonical forms round-trip through the fallback unchanged.
            ("up", "up"),
            ("down", "down"),
            # Non-arrow regression — fallback still lowercases everything.
            ("enter", "enter"),
            ("ENTER", "enter"),
            ("Enter", "enter"),
            ("ctrl", "ctrl"),
            ("CTRL", "ctrl"),
            ("space", "space"),
            # Single-character keys preserve expected lowercasing behavior.
            ("a", "a"),
            ("A", "a"),
        ],
    )
    def test_normalize_key_case_insensitive(self, inp, expected):
        from agent.computers.cua import cuaComputerHandler

        # Bypass __init__ — _normalize_key is pure and needs no interface.
        handler = cuaComputerHandler.__new__(cuaComputerHandler)
        assert handler._normalize_key(inp) == expected
