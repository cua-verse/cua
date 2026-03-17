# US-OC-007: Tool Registry and Logging Hooks

## Context

Tool assembly is currently inline in `openclaw_agent.py` (line 187) — needs extraction to a centralized function. This is the **sole remaining blocker** for US-OC-008 (Agent Loop Integration).

## OpenClaw Design Rationale — Component-by-Component Analysis

OpenClaw's tool system spans `pi-tools.ts` (assembly), `pi-tools.schema.ts` (normalization), `pi-tools.policy.ts` (allow/deny), `pi-tools.before-tool-call.ts` (hooks), and `pi-tools.abort.ts` (cancellation). Here's every component and its disposition:

### 1. Tool Assembly (`createOpenClawCodingTools` in `pi-tools.ts`)
**OpenClaw**: ~550-line function that builds a tool list from base coding tools (read, write, edit, exec, process), swapping in sandboxed/host variants based on sandbox config. Takes ~30 options (agentId, sessionKey, modelProvider, sandbox context, etc.). Returns `AnyAgentTool[]`.

**CUA equivalent**: `ComputerAgent.__init__(tools=[...])` accepts a raw list. Our tools are fixed: Computer (CUA SDK), MilestoneTool (CUA SDK), and 3 memory tools (ours).

**Decision: KEEP (simplified)**. Extract the inline 5-tool list into `build_tools(session, memory_store)`. No variants, no sandbox swapping — CUA has one environment (remote VM). The function is trivial but centralizes tool construction for US-OC-008.

### 2. Tool Schema Normalization (`normalizeToolParameters` in `pi-tools.schema.ts`)
**OpenClaw**: Normalizes JSON schemas per model provider:
- Gemini: strips unsupported keywords (`cleanSchemaForGemini`)
- xAI: strips validation-constraint keywords (`stripXaiUnsupportedKeywords`)
- OpenAI: forces top-level `type: "object"` (rejects root unions)
- Anthropic: expects full JSON Schema draft 2020-12 (no-op)
- Flattens `anyOf`/`oneOf` unions into merged object schemas for cross-provider portability

**CUA equivalent**: `ComputerAgent._process_tools()` (agent.py:349-376) extracts `{name, description, parameters}` from each `BaseTool`. Then `_prepare_tools_for_anthropic()` (anthropic.py:92-116) converts `parameters` → `input_schema` for the Anthropic API. CUA also has an OpenAI loop (`loops/openai.py`) that formats differently.

**Decision: DROP**. CUA already normalizes schemas per provider in its loop implementations. Our `BaseTool` subclasses define `.parameters` as standard JSON Schema objects. The CUA SDK handles the provider-specific translation. OpenClaw needs this because it sends tools through `pi-agent` directly to multiple providers; CUA's `ComputerAgent` abstracts this away.

### 3. Tool Policy Pipeline (`pi-tools.policy.ts`)
**OpenClaw**: Multi-layer allow/deny filtering:
- Profile policy (user-defined tool profiles)
- Provider-specific policy (per model provider)
- Agent-specific policy (per agent config)
- Group policy (per channel/group)
- Sandbox policy (sandbox restrictions)
- Subagent policy (depth-based deny lists — leaf agents can't spawn)
- Owner-only policy (restrict dangerous tools to account owners)
- Message provider policy (deny certain tools for voice channels)

Each layer can allow/deny tools by name/glob pattern. Tools pass through all layers; a deny at any level removes the tool.

**CUA equivalent**: No policy system. All tools are pre-approved by the benchmark harness author.

**Decision: DROP**. OpenClaw is a multi-tenant chat platform where untrusted users can trigger tool calls across channels. CUA is a benchmark framework where the harness author controls the tool list. There's no trust boundary to enforce. `max_steps` provides the only guardrail needed.

### 4. Before-Tool-Call Hooks (`wrapToolWithBeforeToolCallHook` in `pi-tools.before-tool-call.ts`)
**OpenClaw**: Wraps every tool's `execute()` method with a pre-call hook that:
- **Loop detection**: Detects repeated identical tool calls (same name + params), warns or blocks at configurable thresholds
- **Plugin hooks**: Runs user-defined `before_tool_call` plugin hooks (can block or modify params)
- **Adjusted params tracking**: Stores hook-modified params keyed by `toolCallId` for downstream audit
- **Outcome recording**: Records success/error per call for loop detection state

**CUA equivalent**: `AsyncCallbackHandler.on_function_call_start(item)` is called before execution, `on_function_call_end(item, result)` after. These are observer-only — they can't block or modify the call.

**Decision: KEEP (adapted as logging)**. We use CUA's native callback system for **observability only** — log tool name, params, result summary, and duration. We don't need blocking/modification because:
- Loop detection: `max_steps` is sufficient; the agent won't run indefinitely
- Plugin hooks: no plugin system
- Param modification: tools accept params as-is

The `ToolLoggingCallback` uses `on_function_call_start/end` for timing and logging.

### 5. Abort Signal Wrapping (`wrapToolWithAbortSignal` in `pi-tools.abort.ts`)
**OpenClaw**: Wraps every tool's `execute()` to check an `AbortSignal` before and during execution. Allows cancelling long-running tool calls when a session is terminated.

**CUA equivalent**: `ComputerAgent` handles run lifecycle internally. The benchmark harness controls `max_steps` and can break from the `async for` loop.

**Decision: DROP**. CUA's run model is `async for result in agent.run()` — we break from the loop when done. No need for per-tool abort signaling.

### 6. Tool Type System (`pi-tools.types.ts`)
**OpenClaw**: `AnyAgentTool = AgentTool<any, unknown>` from `@mariozechner/pi-agent-core`. Each tool has `name`, `description`, `parameters` (JSON Schema), and `execute(toolCallId, params, signal, onUpdate)`.

**CUA equivalent**: `BaseTool` (agent/tools/base.py) with `name`, `description`, `parameters` properties and `execute(params)` method. Computer objects are duck-typed via `is_agent_computer()`.

**Decision: ALREADY HANDLED**. Our memory tools already extend `BaseTool`. The type system difference is just Python vs TypeScript — functionally equivalent.

### 7. Tool Summary Extraction (for system prompt)
**OpenClaw**: Tools are listed in the system prompt by iterating the tool array and extracting names/descriptions (done in `system-prompt.ts`).

**CUA equivalent**: We currently do this inline in `openclaw_agent.py` (lines 189-193) with an `isinstance(tool, BaseTool)` guard to skip the Computer object.

**Decision: KEEP (extract to helper)**. Move to `get_tool_summaries(tools)` in `tools.py` for reuse.

## Summary Table

| Component | OpenClaw | CUA Equivalent | Decision |
|-----------|----------|---------------|----------|
| Tool assembly | `createOpenClawCodingTools` (550 LOC) | Inline list in `openclaw_agent.py` | **KEEP** — extract to `build_tools()` |
| Schema normalization | `normalizeToolParameters` (per-provider) | `_process_tools()` + per-loop formatters | **DROP** — CUA SDK handles it |
| Policy pipeline | Multi-layer allow/deny | None (all pre-approved) | **DROP** — no trust boundary |
| Before-tool-call hooks | `wrapToolWithBeforeToolCallHook` (block/modify) | `AsyncCallbackHandler` (observe-only) | **ADAPT** — logging callback only |
| Abort signal | `wrapToolWithAbortSignal` | `max_steps` + loop break | **DROP** — different run model |
| Tool type system | `AgentTool<P, R>` | `BaseTool` (ABC) | **ALREADY HANDLED** |
| Tool summaries | In system-prompt builder | Inline in agent | **KEEP** — extract to helper |

## Implementation Plan

### File: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/tools.py` (NEW)

#### 1. `build_tools(session, memory_store) -> list`
Centralized tool assembly. Takes session (for Computer + MilestoneTool) and memory_store (for 3 memory tools). Returns the 5-tool list.

#### 2. `get_tool_summaries(tools) -> dict[str, str]`
Extracts `{name: description}` from BaseTool instances, filtering out Computer.

#### 3. `ToolLoggingCallback(AsyncCallbackHandler)`
- `on_function_call_start(item)`: logs `[Tool] <name>(<truncated_args>)`, records `time.monotonic()` keyed by `call_id`
- `on_function_call_end(item, result)`: logs `[Tool] <name> -> <result_summary> (<duration>ms)`
- Result summary: extract output string, truncate to 100 chars

### File: `openclaw/__init__.py` — add exports

### File: `openclaw_agent.py` — refactor
Replace inline tool assembly (lines 154-193) with `build_tools()` + `get_tool_summaries()`. Add `ToolLoggingCallback()` to callbacks list.

### File: `tests/test_openclaw_tools.py` (NEW)
- `build_tools()` returns 5 tools with correct types
- `get_tool_summaries()` filters out Computer, includes 4 BaseTools
- `ToolLoggingCallback` logs start/end with timing
- Truncation, missing call_id handling

## Verification
1. `uv run ruff check .` — lint passes
2. `uv run pytest tests/test_openclaw_tools.py -v` — all tests pass
3. `run_magic_tower.sh 50` — tool logging visible, tools in trajectory
