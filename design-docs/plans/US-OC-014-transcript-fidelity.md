# US-OC-014: Transcript Fidelity — Session JSONL Reproduction

## Context

Our session transcript already logs bidirectional messages (user + assistant + tool results)
via `SessionManager.append_message()`. This story verifies our transcript format against the
actual OpenClaw session JSONL and documents the differences.

## Golden Reference

`docs/openclaw_ref/openclaw_session.jsonl` — real OpenClaw session file captured from a live instance.

### OpenClaw Session JSONL Format (from golden reference)

**Entry types** (5 total):
| Type | Count | Description |
|------|-------|-------------|
| `session` | 1 | Session header (version=3, cwd) |
| `model_change` | 1 | Provider/model switch (`provider`, `modelId`) |
| `thinking_level_change` | 1 | Thinking level (`thinkingLevel`) |
| `custom` | 22 | Extensible entries (`customType` discriminator): `model-snapshot`, `openclaw.cache-ttl` |
| `message` | 100 | Bidirectional messages (user/assistant/toolResult) |

**Message roles** (3 total):
| Role | Count | Keys |
|------|-------|------|
| `user` | 21 | `role`, `content`, `timestamp` |
| `assistant` | 50 | `role`, `content`, `api`, `provider`, `model`, `usage`, `stopReason`, `timestamp` |
| `toolResult` | 29 | `role`, `toolCallId`, `toolName`, `content`, `isError`, `timestamp` |

**Assistant content block types**: `thinking`, `text`, `toolCall`

**toolCall block shape**: `{type: "toolCall", id, name, arguments}` (arguments is a dict, not a string)

**Usage shape**: `{input, output, cacheRead, cacheWrite, totalTokens, cost: {input, output, cacheRead, cacheWrite, total}}`

### Our Current Format (comparison)

**Entry types**: `session` (version=1), `message`, `compaction`
- We have `compaction` (OpenClaw doesn't store this in session JSONL)
- We lack `model_change`, `thinking_level_change`, `custom`

**Message roles**: `user`, `assistant`, `tool`
- OpenClaw uses `toolResult` role with `toolCallId`/`toolName` top-level fields
- We use `tool` role with content blocks containing `tool_use_id`

**Assistant content block types**: `text`, `function_call`, `computer_call`
- OpenClaw uses `toolCall` (with `arguments` as dict)
- We use `function_call`/`computer_call` (CUA naming, `arguments` as string)

**Usage shape**: `{input, output, total, cost}` (flat)
- OpenClaw has nested cost breakdown with cache fields

### What We Already Reproduce Correctly

1. **Bidirectional logging**: Both what the user/tool sent and what the assistant returned
2. **ParentId chain**: Entry linking via `id`/`parentId`
3. **Session headers**: Run boundary markers
4. **Content block arrays**: Structured content (not flat strings)
5. **API metadata on assistant messages**: `api`, `usage`, `stopReason`

### Deliberate Differences (CUA adaptations)

1. **`function_call`/`computer_call` vs `toolCall`**: CUA uses Responses API naming; OpenClaw uses Anthropic Messages API naming. Both are valid — our replay pipeline handles the CUA names.
2. **`tool` role vs `toolResult` role**: CUA groups tool results as content blocks; OpenClaw uses a dedicated role with flat fields. Our `_unnest_tool_blocks` handles conversion.
3. **`compaction` entry type**: CUA-specific — marks compaction boundaries for cross-run replay.
4. **Flat usage vs nested cost**: Sufficient for our observability needs.

## What This Story Confirms

Our current session transcript already faithfully reproduces OpenClaw's bidirectional message logging pattern. The differences are naming conventions (CUA vs Anthropic API) and CUA-specific additions (compaction entries), not missing data.

**No code changes needed** — the transcript already stores both directions of conversation.

## Future Work: Full API Payload Logging

OpenClaw optionally captures the full preprocessed API request payload (all messages sent to
the model including ImageRetentionCallback modifications) via a **separate** mechanism:

- `openclaw/src/agents/anthropic-payload-log.ts` — logs to `anthropic-payload.jsonl` when
  `OPENCLAW_ANTHROPIC_PAYLOAD_LOG=true`
- `openclaw/src/agents/cache-trace.ts` — records full message arrays at pipeline stages

This is **NOT** part of the session JSONL. If we want to reproduce this (for debugging
ImageRetentionCallback behavior or replay fidelity), it should be a separate optional log
file, not embedded in the session transcript. Tracked as potential future story.

## Files

- `docs/openclaw_ref/openclaw_session.jsonl` — golden reference (real OpenClaw session)
- `docs/plan/US-OC-014-transcript-fidelity.md` — this design doc
