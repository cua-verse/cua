# US-OC-036: Post-Compaction Message Ordering

## Context

After compaction, `_build_compacted_items()` rebuilds the items list as `[user(summary), ...kept_messages]`. The `kept_messages` can end with an assistant message (e.g., the model's last response before compaction triggered). Models like Opus 4.6 that don't support assistant message prefill will reject API calls where the final message has `role=assistant`. This was previously masked by US-OC-023's tool_use pairing issues.

## OpenClaw Design Rationale

**What OpenClaw Does**: OpenClaw's `replaceMessages()` operates within a persistent session where the agent loop always continues with the model generating a new response — the framework guarantees a user/tool message follows.

**What We Keep**: The compaction summary as a user message at the start (already implemented).

**What We Drop**: OpenClaw doesn't need an explicit tail guard because its session model prevents this state.

**Key Difference from OpenClaw**: CUA's items list is directly converted to completion messages. After compaction + ImageRetentionCallback stripping old screenshots, the final item can be `role=assistant`. We need an explicit guard.

## Implementation

### Fix: `_build_compacted_items()` in `agent_loop.py`

After building `[user(summary), ...kept_messages]`, check if the last item has `role=assistant`. If so, append:

```python
{"role": "user", "content": "[Continue from where you left off.]"}
```

**File**: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/agent_loop.py`

### Unit test: `tests/test_message_ordering.py`

5 test cases covering trailing assistant, trailing user, trailing tool, empty kept, and single assistant.

### VM test (Level 2)

`CONTEXT_WINDOW_OVERRIDE=35000 bash run_magic_tower.sh 50 anthropic/claude-opus-4-6`
