# US-OC-017: Custom Loop Architecture — Replace Stop-Compact-Resume

## Problem

The current agent harness uses a **stop-compact-resume** pattern for context overflow:
CUA's `ComputerAgent.run()` is opaque — we can't inject compaction mid-run. So we
break out of the async generator, compact the transcript, create a new `ComputerAgent`,
and resume. This causes:

1. **State discontinuity** — the new agent loses all internal CUA loop state
2. **Compaction summary duplication** (discovered in US-OC-022) — see below
3. **Complexity** — `_compact_and_rebuild` rebuilds agent, instruction, and callbacks

### Compaction Summary Duplication Bug

When replaying prior history AND the session has compaction summaries:

- `build_replay_messages()` converts compaction entries from the JSONL transcript into
  `[Compaction summary]` assistant messages (via `firstKeptEntryId` cut logic)
- `_create_compacted_instruction()` ALSO loads all summaries from `state.json` and
  prepends them to the task instruction

Both go into the same API call → **25 summaries × ~4K chars each ≈ 103K chars of
redundant context** in the instruction message.

**Root cause**: `_create_compacted_instruction` was designed for the stop-compact-resume
path (where `replay_messages = []` after compaction rebuild). But on cross-run replay,
`replay_messages` already carries the compaction context via `firstKeptEntryId`.

**OpenClaw comparison**: OpenClaw does NOT prepend compaction summaries to the
instruction. The Pi SDK's `SessionManager.open()` loads the transcript with compaction
entries inline, and `session.compact()` modifies the transcript in-place. There is no
separate "summaries in instruction" path. Our `_create_compacted_instruction` has no
OpenClaw equivalent.

## How OpenClaw Handles Compaction (Reference)

```
OpenClaw flow (pi-embedded-runner/compact.ts):
1. SessionManager.open(sessionFile) → loads JSONL transcript
2. sanitizeSessionHistory(session.messages) → sanitize + repair
3. limitHistoryTurns(validated) → trim to N turns
4. sanitizeToolUseResultPairing(truncated) → repair orphans
5. session.agent.replaceMessages(limited) → swap in cleaned messages
6. session.compact(customInstructions) → LLM summarizes old messages,
   writes compaction entry to JSONL with firstKeptEntryId
7. Next run: SessionManager.open() sees compaction entry, replaces
   old messages with summary automatically
```

Key insight: compaction is handled entirely within the transcript. There's no
separate instruction-prepending mechanism.

## Proposed Architecture: Custom Loop

CUA's `ComputerAgent` accepts a `custom_loop` parameter that lets us manage our
own message list. This enables **mid-conversation compaction** without breaking
out of the agent loop.

### Current Flow (Stop-Compact-Resume)

```
agent = ComputerAgent(model, tools, instructions)
while True:
    async for result in agent.run(run_input):
        # process step
        if overflow_cb.needs_compaction:
            break  # ← break out of generator
    if compaction_triggered:
        # summarize transcript
        # create NEW agent with compacted instruction
        agent = ComputerAgent(...)  # ← state lost
        replay_messages = []
    else:
        break  # done
```

### Target Flow (Custom Loop)

```
agent = ComputerAgent(model, tools, instructions, custom_loop=our_loop)
async for result in agent.run(run_input):
    # process step
    if overflow_cb.needs_compaction:
        # compact messages IN-PLACE (no agent rebuild)
        agent.replace_messages(compacted_messages)
        # loop continues with same agent
```

### Changes Required

| Component | Change |
|-----------|--------|
| `openclaw_agent.py` | Replace stop-compact-resume while loop with custom_loop |
| `_create_compacted_instruction` | **Delete entirely** — no longer needed |
| `_compact_and_rebuild` | **Delete entirely** — compaction happens in-place |
| `convert_to_responses_api_items` | Keep — still needed for cross-run replay |
| `build_replay_messages` | Keep — handles cross-run transcript replay with `firstKeptEntryId` |

### Interim Fix (Before Custom Loop)

Until US-OC-017 is implemented, the duplication can be fixed with a one-line guard
in `openclaw_agent.py`:

```python
prior_summaries = session_mgr.get_compaction_summaries()
if prior_summaries and not replay_messages:  # ← skip when replay already has them
    instruction = _create_compacted_instruction(task_description, prior_summaries)
```

This preserves the stop-compact-resume path (where `replay_messages = []`) while
avoiding duplication on cross-run replay.

## Verification

- **Unit**: Existing `test_openclaw_replay.py` tests cover the replay pipeline
- **Integration**: VM test should show reduced input tokens (~80K → ~40K on first turn
  after removing the 103K redundant instruction)
- **Custom loop**: Prototype must pass 50-step VM test with mid-run compaction
