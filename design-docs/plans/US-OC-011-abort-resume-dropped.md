# US-OC-011: Abort/Resume Tracking — DROPPED

## Decision

**Dropped** from the CUA reproduction. OpenClaw's abort cutoff solves a problem that doesn't exist in our architecture.

## Analysis of OpenClaw's Implementation

OpenClaw's abort cutoff is an **inbound message gate**, not a transcript replay filter:

1. **Trigger**: User sends `/stop` → `handleStopCommand` in `commands-session-abort.ts` sets `sessionEntry.abortedLastRun = true` and records `abortCutoffMessageSid` (the Twilio/channel message ID of the stop command) + `abortCutoffTimestamp`.

2. **Consumer**: `get-reply-inline-actions.ts:262-284` — when a **new inbound message** arrives from a channel (Telegram, Discord, etc.), the pipeline checks `shouldSkipMessageByAbortCutoff()`. If the incoming message's SID/timestamp is at or before the cutoff, it's **silently dropped** (returns `reply: undefined`). Once a message arrives after the cutoff, `clearAbortCutoffInSession()` clears the flag.

3. **Purpose**: After `/stop`, messages that were queued in the channel pipeline (sent by users before the stop propagated) need to be ignored. Without this, the agent would process stale queued messages as if they were new requests.

### Key source files

- `openclaw/src/config/sessions/types.ts` — `abortedLastRun`, `abortCutoffMessageSid`, `abortCutoffTimestamp` fields
- `openclaw/src/commands/commands-session-abort.ts` — `handleStopCommand` sets the cutoff
- `openclaw/src/agents/get-reply-inline-actions.ts:262-284` — `shouldSkipMessageByAbortCutoff()` check on inbound messages

## Why It Doesn't Apply to CUA

| OpenClaw concern | CUA equivalent | Verdict |
|---|---|---|
| Inbound message queue from Telegram/Discord/etc. | None — agent is sole actor | No queue to gate |
| `/stop` command from user chat interface | Signals, exceptions, max_steps | Different mechanism entirely |
| Stale queued messages processed after abort | N/A — no external message producers | Problem doesn't exist |
| SID/timestamp dual-track message comparison | UUID-based transcript entry IDs | Incompatible ID scheme |
| Transcript replay after abort | `limit_history_turns` (US-OC-012) already bounds replay | Already solved |

## What Covers the Same Ground

- **US-OC-012 (Transcript Replay)**: `limit_history_turns` caps how many prior turns are replayed, preventing unbounded replay regardless of how a prior run ended.
- **US-OC-006 (Compaction)**: Summaries provide compressed prior context, so even if a run was interrupted mid-compaction, the next run sees a coherent history.
- **CUA framework signals**: `max_steps`, timeout, and exception handling already manage run interruption without needing session-level abort tracking.
