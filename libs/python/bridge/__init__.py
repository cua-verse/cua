"""
cua-bridge - MCP server that exposes an EnvProtocol as MCP tools.

External agents (Claude Code, OpenClaw, etc.) connect to this
MCP server and interact with the environment through standard
MCP tool calls without knowing about EnvProtocol.
"""

from .mcp_bridge import MCPToolBridge

__all__ = ["MCPToolBridge"]
