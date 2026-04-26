"""B2 + B3 ``_handle_item`` back-port.

For CUA SDK pins that predate the openclaw fork, ``ComputerAgent._handle_item``
lacks two branches that the fork ships at ``agent/agent.py`` lines 786-978
(commit ``b420c6e8`` on yixiao-huang/cua, ``openclaw-cua`` branch):

  B2  Dispatch ``function_call`` items where ``name == "computer"`` to the
      computer handler. Mirrors the native ``computer_call`` path.
      Without this, GPT-5.4-style models — and at least one path under
      openclaw's default Anthropic lineup, per the 2026-04-24 empirical
      probe — emit ``function_call`` with ``name="computer"`` and crash on
      older pins' generic tool lookup with ``Function computer not found``.

  B3  When a tool returns ``{"type": "image", "data": b64, "mime_type": m}``,
      emit a sentinel in ``function_call_output`` and append a separate
      user message with ``input_image`` content. Mirrors the
      computer-call screenshot pattern. Used by openclaw's ``ReadFileTool``
      (US-OC-055).

Strategy: subclass ``OpenClawComputerAgent`` (which already overrides
``_handle_item`` to append a screenshot-path message) and intercept
``function_call`` items locally. Everything else (``message`` /
``computer_call``) delegates to ``super()`` so the parent's post-processing
and the unchanged branches keep working. At pins that already include the
B2/B3 branches, the override is a no-op (it duplicates the parent's
behavior).

Drift warning: this re-implements ~150 lines of ``agent.py:_handle_item``
body. When the fork's ``_handle_item`` evolves (new item type, new branch),
diff against ``agent/agent.py:786-978`` of the openclaw fork on every
change.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any, Dict, List, Optional

from agent.agent import assert_callable_with
from agent.computers.base import AsyncComputerHandler
from agent.responses import make_tool_error_item
from agent.tools.base import BaseTool
from agent.types import ToolError
from core.telemetry import is_telemetry_enabled, record_event
from cua_bench.agents.openclaw.agent_loop import OpenClawComputerAgent


# Map of computer-action name → the param keys the action accepts.
# Mirrors fork ``agent.py`` lines 798-810. Used to filter the JSON
# arguments blob a model emits for ``function_call name="computer"``.
_COMPUTER_ACTION_PARAMS: Dict[str, List[str]] = {
    "screenshot": [],
    "click": ["x", "y", "button"],
    "double_click": ["x", "y"],
    "right_click": ["x", "y"],
    "type": ["text"],
    "keypress": ["keys"],
    "scroll": ["x", "y", "scroll_x", "scroll_y"],
    "move": ["x", "y"],
    "drag": ["start_x", "start_y", "end_x", "end_y"],
    "wait": ["seconds", "ms"],
    "terminate": ["status"],
}


class OpenClawImageAwareComputerAgent(OpenClawComputerAgent):
    """OpenClawComputerAgent with B2/B3 ``_handle_item`` dispatch.

    Replaces the parent's handling of ``function_call`` items. Other item
    types delegate to ``super()`` so the screenshot-path message append
    and computer_call branch still work as in the fork.
    """

    async def _handle_item(
        self,
        item: Dict[str, Any],
        computer: Optional[AsyncComputerHandler] = None,
        ignore_call_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        if item.get("type") != "function_call":
            return await super()._handle_item(item, computer, ignore_call_ids)

        call_id = item.get("call_id")
        if ignore_call_ids and call_id and call_id in ignore_call_ids:
            return []

        # Reset before dispatch so the post-process below can detect a
        # screenshot saved by ``_on_screenshot`` during the call.
        self._last_screenshot_path = None

        try:
            result = await self._dispatch_function_call(item, computer)
        except ToolError as e:
            return [make_tool_error_item(repr(e), call_id)]

        if self._last_screenshot_path and result:
            result.append(
                {
                    "type": "message",
                    "role": "user",
                    "content": f"[Screenshot saved to: {self._last_screenshot_path}]",
                }
            )
            self._last_screenshot_path = None
        return result

    async def _dispatch_function_call(
        self,
        item: Dict[str, Any],
        computer: Optional[AsyncComputerHandler],
    ) -> List[Dict[str, Any]]:
        await self._on_function_call_start(item)

        if item.get("name") == "computer" and computer:
            result = await self._dispatch_b2_computer_call(item, computer)
            await self._on_function_call_end(item, result)
            return result

        # Regular function dispatch — mirrors fork agent.py:900-919.
        function = self._get_tool(item.get("name"))
        if not function:
            raise ToolError(f"Function {item.get('name')} not found")

        args = json.loads(item.get("arguments"))
        if isinstance(function, BaseTool):
            tool_result: Any = function.call(args)
        else:
            assert_callable_with(function, **args)
            if inspect.iscoroutinefunction(function):
                tool_result = await function(**args)
            else:
                tool_result = await asyncio.to_thread(function, **args)

        if self.telemetry_enabled and is_telemetry_enabled():
            record_event(
                "agent_tool_executed",
                {"tool_type": "function", "tool_name": item.get("name")},
            )

        # B3: image-shaped tool return → sentinel + image_message.
        if (
            isinstance(tool_result, dict)
            and tool_result.get("type") == "image"
            and isinstance(tool_result.get("data"), str)
            and isinstance(tool_result.get("mime_type"), str)
        ):
            sentinel: Dict[str, Any] = {
                "success": tool_result.get("success", True),
                "read_image": True,
                "mime_type": tool_result["mime_type"],
            }
            if isinstance(tool_result.get("text"), str):
                sentinel["text"] = tool_result["text"]
            call_output = {
                "type": "function_call_output",
                "call_id": item.get("call_id"),
                "output": json.dumps(sentinel),
            }
            image_message = {
                "role": "user",
                "content": [
                    {
                        "type": "input_image",
                        "image_url": f"data:{tool_result['mime_type']};base64,{tool_result['data']}",
                    }
                ],
            }
            wrapped = [call_output, image_message]
            await self._on_function_call_end(item, wrapped)
            return wrapped

        call_output = {
            "type": "function_call_output",
            "call_id": item.get("call_id"),
            "output": str(tool_result),
        }
        wrapped = [call_output]
        await self._on_function_call_end(item, wrapped)
        return wrapped

    async def _dispatch_b2_computer_call(
        self,
        item: Dict[str, Any],
        computer: AsyncComputerHandler,
    ) -> List[Dict[str, Any]]:
        """B2: route ``function_call name="computer"`` to the computer handler.

        Mirrors fork ``agent.py`` lines 791-898: parse the JSON action,
        execute it, screenshot, return ``function_call_output`` plus a
        user message with the screenshot.
        """
        args = json.loads(item.get("arguments", "{}"))
        action_type = args.get("action")
        if not action_type:
            raise ToolError("Computer function call missing 'action' argument")

        relevant_params = _COMPUTER_ACTION_PARAMS.get(action_type, [])
        action_args: Dict[str, Any] = {}
        for k, v in args.items():
            if k == "action":
                continue
            if k in relevant_params or action_type not in _COMPUTER_ACTION_PARAMS:
                if v is not None and v != "" and v != []:
                    action_args[k] = v

        computer_method = getattr(computer, action_type, None)
        if not computer_method:
            raise ToolError(f"Unknown computer action: {action_type}")
        action_result = await computer_method(**action_args)

        if self.telemetry_enabled and is_telemetry_enabled():
            record_event(
                "computer_action_executed",
                {"action_type": action_type},
            )
            record_event(
                "agent_tool_executed",
                {"tool_type": "computer", "tool_name": action_type},
            )

        is_terminate = action_type == "terminate" or (
            isinstance(action_result, dict) and action_result.get("terminated")
        )

        if is_terminate:
            output_content = json.dumps(action_result if action_result else {"terminated": True})
            return [
                {
                    "type": "function_call_output",
                    "call_id": item.get("call_id"),
                    "output": output_content,
                }
            ]

        if self.screenshot_delay and self.screenshot_delay > 0:
            await asyncio.sleep(self.screenshot_delay)
        screenshot_base64 = await computer.screenshot()
        await self._on_screenshot(screenshot_base64, "screenshot_after")

        # ``action="screenshot"`` returns raw base64 — short-circuit to
        # avoid ballooning the transcript by ~58K tokens per call (and to
        # let ``only_n_most_recent_images`` pruning still apply via the
        # ``image_url`` block we emit below). Same rationale as fork
        # lines 866-879.
        if action_type == "screenshot" or action_result is None:
            output_content = json.dumps({"success": True, "screenshot_captured": True})
        else:
            output_content = json.dumps(action_result)
        call_output = {
            "type": "function_call_output",
            "call_id": item.get("call_id"),
            "output": output_content,
        }
        image_message = {
            "role": "user",
            "content": [
                {
                    "type": "input_image",
                    "image_url": f"data:image/png;base64,{screenshot_base64}",
                }
            ],
        }
        return [call_output, image_message]
