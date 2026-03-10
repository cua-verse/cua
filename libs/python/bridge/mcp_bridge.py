"""MCP server that exposes a DesktopEnv as MCP tools.

External agents (Claude Code, OpenClaw, etc.) connect to this
MCP server and interact with the environment through standard
MCP tool calls. They never see the DesktopEnv or OpenEnv API directly.

Tools exposed:
  - screenshot() -> base64 image
  - click(x, y, button="left") -> base64 image
  - double_click(x, y) -> base64 image
  - type_text(text) -> base64 image
  - scroll(x, y, scroll_x, scroll_y) -> base64 image
  - keypress(keys) -> base64 image
  - drag(path) -> base64 image
  - move(x, y) -> base64 image
  - wait(ms=1000) -> base64 image
  - done() -> signals task completion
  - get_screen_info() -> {width, height, environment}

Every action tool returns the screenshot AFTER the action,
so the agent always sees the result of what it did.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, TYPE_CHECKING

from env.protocol import DesktopAction

if TYPE_CHECKING:
    from env.desktop import DesktopEnv


class MCPToolBridge:
    """Wraps a DesktopEnv as an MCP server with GUI action tools.

    Usage:
        bridge = MCPToolBridge(env, port=9500)
        mcp_url = await bridge.start()
        # External agent connects to mcp_url and calls tools
        await bridge.stop()
    """

    def __init__(
        self,
        env: "DesktopEnv",
        host: str = "0.0.0.0",
        port: int = 9500,
    ):
        self.env = env
        self.host = host
        self.port = port
        self._server: Any = None
        self._trajectory: List[Dict[str, Any]] = []

    def create_mcp_server(self):
        """Create and return a FastMCP server with env tools."""
        from fastmcp import FastMCP

        mcp = FastMCP(
            "cua-env",
            host=self.host,
            port=self.port,
            stateless_http=True,
        )
        env = self.env
        trajectory = self._trajectory

        @mcp.tool()
        async def screenshot() -> str:
            """Take a screenshot of the current screen. Returns base64 PNG."""
            obs = await env.observe()
            return obs.screenshot or ""

        @mcp.tool()
        async def click(x: int, y: int, button: str = "left") -> str:
            """Click at coordinates (x, y). Returns screenshot after click."""
            action = DesktopAction(
                type="click", params={"x": x, "y": y, "button": button}
            )
            obs = await env.step_async(action)
            trajectory.append({"action": "click", "x": x, "y": y, "button": button})
            return obs.screenshot or ""

        @mcp.tool()
        async def double_click(x: int, y: int) -> str:
            """Double-click at coordinates (x, y). Returns screenshot after."""
            action = DesktopAction(type="double_click", params={"x": x, "y": y})
            obs = await env.step_async(action)
            trajectory.append({"action": "double_click", "x": x, "y": y})
            return obs.screenshot or ""

        @mcp.tool()
        async def type_text(text: str) -> str:
            """Type the given text. Returns screenshot after typing."""
            action = DesktopAction(type="type", params={"text": text})
            obs = await env.step_async(action)
            trajectory.append({"action": "type", "text": text})
            return obs.screenshot or ""

        @mcp.tool()
        async def scroll(
            x: int, y: int, scroll_x: int, scroll_y: int
        ) -> str:
            """Scroll at (x, y) by (scroll_x, scroll_y). Returns screenshot after."""
            action = DesktopAction(
                type="scroll",
                params={
                    "x": x,
                    "y": y,
                    "scroll_x": scroll_x,
                    "scroll_y": scroll_y,
                },
            )
            obs = await env.step_async(action)
            trajectory.append(
                {
                    "action": "scroll",
                    "x": x,
                    "y": y,
                    "scroll_x": scroll_x,
                    "scroll_y": scroll_y,
                }
            )
            return obs.screenshot or ""

        @mcp.tool()
        async def keypress(keys: list[str]) -> str:
            """Press key combination (e.g. ["ctrl", "c"]). Returns screenshot after."""
            action = DesktopAction(type="keypress", params={"keys": keys})
            obs = await env.step_async(action)
            trajectory.append({"action": "keypress", "keys": keys})
            return obs.screenshot or ""

        @mcp.tool()
        async def drag(path: list[dict]) -> str:
            """Drag along path [{"x": .., "y": ..}, ...]. Returns screenshot after."""
            action = DesktopAction(type="drag", params={"path": path})
            obs = await env.step_async(action)
            trajectory.append({"action": "drag", "path": path})
            return obs.screenshot or ""

        @mcp.tool()
        async def move(x: int, y: int) -> str:
            """Move cursor to (x, y). Returns screenshot after."""
            action = DesktopAction(type="move", params={"x": x, "y": y})
            obs = await env.step_async(action)
            trajectory.append({"action": "move", "x": x, "y": y})
            return obs.screenshot or ""

        @mcp.tool()
        async def wait(ms: int = 1000) -> str:
            """Wait for ms milliseconds. Returns screenshot after."""
            action = DesktopAction(type="wait", params={"ms": ms})
            obs = await env.step_async(action)
            trajectory.append({"action": "wait", "ms": ms})
            return obs.screenshot or ""

        @mcp.tool()
        async def done() -> str:
            """Signal that the task is complete."""
            trajectory.append({"action": "done"})
            return json.dumps({"status": "done", "steps": len(trajectory)})

        @mcp.tool()
        async def get_screen_info() -> str:
            """Get screen dimensions and environment type."""
            env_state = env.state
            dims = env_state.dimensions
            return json.dumps(
                {
                    "width": dims[0] if dims else 1024,
                    "height": dims[1] if dims else 768,
                    "environment": env_state.environment_type or "linux",
                }
            )

        return mcp

    async def start(self) -> str:
        """Start the MCP server. Returns the MCP endpoint URL."""
        import asyncio

        import uvicorn

        mcp = self.create_mcp_server()
        app = mcp.streamable_http_app()
        config = uvicorn.Config(
            app, host=self.host, port=self.port, log_level="warning"
        )
        self._server = uvicorn.Server(config)
        # Run server in background task
        asyncio.create_task(self._server.serve())
        # Brief wait for server startup
        await asyncio.sleep(0.5)
        return f"http://{self.host}:{self.port}/mcp"

    async def stop(self):
        """Stop the MCP server."""
        if self._server:
            await self._server.shutdown()

    @property
    def trajectory(self) -> List[Dict[str, Any]]:
        """Get a copy of the recorded action trajectory."""
        return list(self._trajectory)
