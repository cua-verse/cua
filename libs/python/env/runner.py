"""Evaluation runner: pairs agents with environments and collects metrics.

EvalRunner handles in-process agents (CUALoopAgent or any AgentProtocol).
ExternalAgentRunner handles out-of-process agents (Claude Code, etc.)
that interact via the MCP ToolBridge.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, TYPE_CHECKING

from .protocol import TaskSpec
from .agent_protocol import AgentProtocol, AgentResult

if TYPE_CHECKING:
    from .desktop import DesktopEnv


@dataclass
class EvalMetrics:
    """Metrics collected from a single task evaluation."""

    task: TaskSpec
    result: AgentResult
    wall_time_seconds: float
    num_steps: int
    metadata: Dict[str, Any] = field(default_factory=dict)


class EvalRunner:
    """Runs an in-process agent against an environment on a list of tasks.

    Usage:
        runner = EvalRunner(agent=my_agent, env=my_env)
        results = await runner.run_tasks([task1, task2, ...])
    """

    def __init__(self, agent: AgentProtocol, env: "DesktopEnv"):
        self.agent = agent
        self.env = env

    async def run_task(self, task: TaskSpec) -> EvalMetrics:
        """Run a single task and return metrics."""
        start = time.time()
        result = await self.agent.run(task, self.env)
        elapsed = time.time() - start
        return EvalMetrics(
            task=task,
            result=result,
            wall_time_seconds=elapsed,
            num_steps=len(result.trajectory),
        )

    async def run_tasks(self, tasks: List[TaskSpec]) -> List[EvalMetrics]:
        """Run multiple tasks sequentially and return all metrics."""
        results = []
        for task in tasks:
            metrics = await self.run_task(task)
            results.append(metrics)
        return results


class ExternalAgentRunner:
    """Runs an external agent (via MCP ToolBridge) against an environment.

    The external agent is launched as a subprocess and communicates
    with the environment through MCP tools exposed by the ToolBridge.

    Usage:
        runner = ExternalAgentRunner(env=my_env)
        result = await runner.run(
            task=task,
            agent_cmd=["claude", "--mcp-config", "env_bridge.json", "-p", "{instruction}"],
        )

    The placeholders {mcp_url} and {instruction} in agent_cmd will be
    replaced with the actual MCP server URL and task instruction.
    """

    def __init__(self, env: "DesktopEnv", bridge_port: int = 9500):
        self.env = env
        self.bridge_port = bridge_port

    async def run(
        self,
        task: TaskSpec,
        agent_cmd: List[str],
        timeout: float = 300.0,
    ) -> EvalMetrics:
        """Launch external agent subprocess with MCP bridge and collect metrics."""
        import asyncio

        from bridge import MCPToolBridge

        # 1. Reset env via OpenEnv interface
        await self.env.reset_async()

        # 2. Start MCP bridge
        bridge = MCPToolBridge(self.env, port=self.bridge_port)
        mcp_url = await bridge.start()

        # 3. Launch external agent with placeholders substituted
        cmd = [
            c.replace("{mcp_url}", mcp_url).replace(
                "{instruction}", task.instruction
            )
            for c in agent_cmd
        ]

        start = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.terminate()
        finally:
            await bridge.stop()

        elapsed = time.time() - start
        trajectory = bridge.trajectory

        return EvalMetrics(
            task=task,
            result=AgentResult(
                success=any(t.get("action") == "done" for t in trajectory),
                message=f"External agent finished in {elapsed:.1f}s",
                trajectory=trajectory,
            ),
            wall_time_seconds=elapsed,
            num_steps=len(trajectory),
        )
