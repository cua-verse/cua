# CUA Agent-Environment Decoupling: Design Notes

## Problem

The CUA framework has tight coupling between agent logic and environment logic. `ComputerAgent.run()` owns both the agent loop (`predict_step()`) and environment execution (`_handle_item()` → `computer_handler.click/type/screenshot`). This makes it impossible to:

1. **Evaluate external agents** (Claude Code, OpenClaw) that have their own architectures and don't implement `predict_step()`.
2. **Swap environments independently** from agents (local desktop vs. cloud VM vs. browser).
3. **Use a standardized benchmark protocol** where the environment is the evaluation boundary.

## Design Principle

**Additive, not destructive.** All existing code (`ComputerAgent`, 20+ agent loops, `AsyncComputerHandler`) continues to work unchanged. The new packages (`env/`, `bridge/`) are a parallel integration path.

**Zero existing files modified.** ~750 lines of new code across 10 new files.

## Why OpenEnv?

The initial implementation defined a custom `EnvProtocol` with custom `Action`, `Observation`, `StepResult` types. During review, we identified that the [envbeats](https://github.com/meta-pytorch/OpenEnv) ecosystem (used in `https://github.com/agentbeats/envbeats`) already defines a standard environment protocol via `openenv-core`:

- `Action(BaseModel)` — base action with metadata
- `Observation(BaseModel)` — base observation with `done`, `reward`, `metadata`
- `State(BaseModel)` — environment state with `episode_id`, `step_count`
- `StepResult[ObsT]` — generic step result (observation + reward + done)
- `Environment[ActT, ObsT, StateT]` — abstract base class with `reset()`, `step()`, `state`

Rather than maintaining a parallel type system, we chose to **extend OpenEnv directly**:

| Custom (removed) | OpenEnv-based (current) |
|---|---|
| `Action` (dataclass) | `DesktopAction(openenv.Action)` — adds `type`, `params` |
| `Observation` (dataclass) | `DesktopObservation(openenv.Observation)` — adds `screenshot` |
| `StepResult` (dataclass) | `StepResult` from `openenv.core.client_types` (re-exported) |
| `EnvProtocol` (Protocol) | `Environment` ABC from `openenv.core.env_server.interfaces` |
| — | `DesktopState(openenv.State)` — adds `dimensions`, `environment_type` |

This means envbeats assessors can serve a `DesktopEnv` natively, and any `EnvClient` can interact with CUA environments through the standard OpenEnv protocol.

## Architecture

```
  ┌───────────────────────┐        ┌──────────────────────────────┐
  │ CUALoopAgent          │        │ DesktopEnv                   │
  │ wraps existing loops  │───────>│ extends openenv.Environment  │
  │ into AgentProtocol    │  calls │ wraps AsyncComputerHandler   │
  └───────────────────────┘  step  └──────────────┬───────────────┘
                              _async              │
  ┌───────────────────────┐        ┌──────────────▼───────────────┐
  │ External Agent        │        │ MCPToolBridge                │
  │ (Claude Code, etc.)   │        │ MCP server exposing          │
  │ speaks MCP tools      │───────>│ DesktopEnv as tools          │
  └───────────────────────┘  MCP   └──────────────────────────────┘

  ┌───────────────────────┐
  │ envbeats assessor     │        (DesktopEnv IS an OpenEnv
  │ uses EnvClient        │───────> Environment — no adapter needed)
  └───────────────────────┘
```

## Package Structure

```
libs/python/
├── env/                          # NEW — environment protocol package
│   ├── protocol.py               # DesktopAction/Observation/State (extend OpenEnv)
│   ├── desktop.py                # DesktopEnv (extends openenv.Environment)
│   ├── agent_protocol.py         # AgentProtocol, CUALoopAgent, EnvBackedHandler
│   ├── runner.py                 # EvalRunner, ExternalAgentRunner
│   ├── shortcuts.py              # make_env_from_computer(), make_agent_from_model()
│   ├── __init__.py
│   └── pyproject.toml            # depends on cua-agent + openenv-core
├── bridge/                       # NEW — MCP tool bridge package
│   ├── mcp_bridge.py             # MCPToolBridge (MCP server wrapping DesktopEnv)
│   ├── __init__.py
│   └── pyproject.toml            # depends on cua-env + fastmcp + uvicorn
├── agent/agent/                  # UNCHANGED
├── computer/                     # UNCHANGED
└── ...
```

## Key Design Decisions

### DesktopEnv extends OpenEnv's Environment ABC

`DesktopEnv` implements both the OpenEnv server interface (`reset`, `step`, `state` property) and CUA-specific convenience methods (`observe`, `action_spec`, `obs_spec`). The sync methods (`reset`, `step`) delegate to async versions via `asyncio.run()` for OpenEnv framework compatibility; prefer `reset_async` / `step_async` in async contexts.

### Observation carries done/reward (OpenEnv convention)

In OpenEnv, `done` and `reward` live on the `Observation` itself. The server framework reads `obs.done` and `obs.reward` to construct `StepResult[ObsT]` for clients. Our `DesktopObservation` inherits this — `step_async()` returns a `DesktopObservation` with `done=True` for terminate actions.

### EnvBackedHandler for loops that read from computer_handler

Some CUA loops (uitars, gemini, composed_grounded) call `computer_handler.screenshot()` or `get_dimensions()` inside `predict_step()`. `EnvBackedHandler` is a thin shim that implements these read-only methods backed by the env, while raising `NotImplementedError` on action methods (actions go through `env.step_async()` in the outer loop).

### MCPToolBridge returns screenshots after every action

Every MCP tool (click, type, scroll, etc.) returns the post-action screenshot so the external agent always sees the result. This mirrors how `ComputerAgent._handle_item()` takes a screenshot after each action.

## Usage Examples

### Evaluate an existing CUA loop via OpenEnv

```python
from computer import Computer
from env import make_env_from_computer, make_agent_from_model, TaskSpec
from env.runner import EvalRunner

computer = Computer(os_type="macos")
env = await make_env_from_computer(computer)
agent = make_agent_from_model("anthropic/claude-sonnet-4-20250514")

runner = EvalRunner(agent=agent, env=env)
metrics = await runner.run_task(TaskSpec(instruction="Open Safari and go to google.com"))
```

### Serve DesktopEnv for envbeats assessors

```python
from computer import Computer
from env import make_env_from_computer

env = await make_env_from_computer(Computer(os_type="macos"))

# DesktopEnv IS an OpenEnv Environment — serve it directly
# env.reset(), env.step(), env.state all follow the OpenEnv protocol
obs = env.reset()           # DesktopObservation (extends openenv.Observation)
obs = env.step(action)      # DesktopObservation with done/reward
state = env.state           # DesktopState (extends openenv.State)
```

### Expose environment to Claude Code via MCP

```python
from env import make_env_from_computer, TaskSpec
from bridge import MCPToolBridge

env = await make_env_from_computer(Computer(os_type="macos"))
await env.reset_async()

bridge = MCPToolBridge(env, port=9500)
mcp_url = await bridge.start()  # "http://0.0.0.0:9500/mcp"
# Claude Code connects and calls: screenshot(), click(x,y), type_text(text), done()
```
