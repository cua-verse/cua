# US-OC-053 — Tool Policy Resolver (OpenClaw `resolveEffectiveToolPolicy` port)

## Context

US-OC-052 added two boolean kwargs (`disable_main_computer`, `disable_delegate_gui`) that each gate a specific tool. That covers today's needs but doesn't scale. Anticipated triggers for a proper policy layer:

- **Per-subagent gating.** General subagents need a different tool set than the main agent (no `delegate_general`, no `delegate_gui`, no `subagents` — currently hard-coded in `subagent_session._filter_tools`). A policy object would generalize this.
- **Per-model gating.** Some models can't use certain tools reliably (non-vision models with `analyze_image`, models that fail on nested function-calling).
- **Per-profile presets.** "observe-only" (no mutations), "read-only memory" (memory tools but `memory_write` blocked), "vision-off", etc.
- **User-config-driven allow/deny lists.** Operator-supplied overrides without code changes.

OpenClaw reference: `../openclaw/src/agents/pi-tools.policy.ts`:
- `SUBAGENT_TOOL_DENY_ALWAYS` / `SUBAGENT_TOOL_DENY_LEAF` (L32–54) — hard-coded deny lists keyed by subagent role.
- `resolveSubagentToolPolicy(cfg, depth)` (L81–98) — merges user config (`allow`, `alsoAllow`, `deny`) with role-based base deny.
- `filterToolsByPolicy(tools, policy)` (L121–126) — the single filter applied before tools reach the model.
- `resolveEffectiveToolPolicy(params)` (L300–355) — top-level resolver that picks between agent-level policy, global policy, per-provider policy, explicit and implicit profile overrides, and `alsoAllow` lists.

## Proposed design (Python port)

`openclaw/tool_policy.py` (NEW):

```python
@dataclass(frozen=True)
class ToolPolicy:
    allow: frozenset[str] | None = None   # None = "no allowlist, only deny applies"
    deny: frozenset[str] = frozenset()
    profile: str | None = None            # "full" | "read-only" | "observe-only" | custom

def filter_tools_by_policy(tools: list, policy: ToolPolicy | None) -> list:
    """Mirror of filterToolsByPolicy (pi-tools.policy.ts:121-126)."""

def resolve_main_agent_policy(
    *, config: dict | None, model_provider: str | None, model_id: str | None,
) -> ToolPolicy:
    """Mirror of resolveEffectiveToolPolicy (pi-tools.policy.ts:300-355).
    Merges: global deny + per-provider overrides + config.allow/deny + profile rules."""

def resolve_subagent_policy(
    *, config: dict | None, role: Literal["orchestrator", "leaf"],
) -> ToolPolicy:
    """Mirror of resolveSubagentToolPolicy (pi-tools.policy.ts:81-98).
    Base deny from SUBAGENT_TOOL_DENY_ALWAYS + role-specific extras."""
```

Replace the current ad-hoc filters:
- `disable_main_computer` / `disable_delegate_gui` kwargs in `build_tools` → resolved into a `ToolPolicy` with `deny={"delegate_gui"}` etc. The CLI flags continue to work as sugar that sets `deny` entries.
- `subagent_session._filter_tools` hard-coded name list → `resolve_subagent_policy(role="leaf")` applied via `filter_tools_by_policy`.

Configuration surface: a `tools:` block in whatever config mechanism we adopt (TOML/YAML at `~/.agenthle/config.*`, or env-var driven initially). First cut matching OpenClaw shape:

```yaml
tools:
  profile: full               # full | observe-only | read-only | custom
  allow: [...]                # optional
  alsoAllow: [...]            # merged after profile defaults
  deny: [delegate_gui]
  byProvider:
    openai:
      profile: full
      deny: [analyze_image]
  subagents:
    tools:
      deny: []
      allow: []
```

## Design rationale vs OpenClaw

**What OpenClaw does.** Policy resolution order: agent config → global config → per-provider policy → profile rules → explicit allow/deny → alsoAllow. A single `filterToolsByPolicy` is the only place tool lists are reduced. Subagent policy is a separate resolver keyed on role (orchestrator vs leaf) with a hard-coded base deny list on top of user config.

**What we'd keep.**
- Merge order and resolver shape — port the TypeScript test cases directly.
- Separate resolvers for main agent vs subagents.
- `allow` / `alsoAllow` / `deny` / `profile` vocabulary.
- Single `filter_tools_by_policy` as the one reduction point.

**What we'd drop.**
- Channel/group policy plumbing (`resolveGroupToolPolicy` at `pi-tools.policy.ts:357+`) — OpenClaw-specific (Telegram/Discord/Slack groups). Not applicable to CUA benchmark runs.
- Sandbox tool policy (`pickSandboxToolPolicy`, `sandbox-tool-policy.ts`) — we don't run the agent in a sandbox container.
- Plugin/channel tool gating — we don't have plugins or channels.

**Key differences from OpenClaw.**
- Our tool names differ — the test-case port must adapt names, not just copy assertions.
- The `profile` presets are ours to define (OpenClaw's profiles are tied to their Pi runtime — not directly reusable).

## Stories this unblocks

1. **Subagent policy parity.** Replace the name-list filter in `subagent_session._filter_tools` with `resolve_subagent_policy(role="leaf")`. Tests: general subagent tool list matches the leaf deny set; delegation tools cannot re-enter a subagent even via config.
2. **Per-provider policy.** Scaffold `byProvider` resolution so models that can't handle specific tools (nested function-calling limits, vision requirements) can be gated without code changes.
3. **Observability.** Emit the resolved policy into the transcript once per run (structured JSON) so `/analyze` can answer "why didn't the agent use X?" with "deny list" rather than "model choice."

## Dependencies

- **US-OC-052** must be landed — its two kwargs become the smallest legal policy (`deny={"delegate_gui"}` etc.). The policy layer is a generalization, not a replacement.
- A config-loading convention. If we adopt TOML or YAML at this point, the resolver plugs into it. Otherwise, env-var-driven as a first cut.

## Acceptance sketch

- `ToolPolicy` + `filter_tools_by_policy` unit-tested against OpenClaw's test cases (port the assertions from `../openclaw/src/agents/pi-tools.policy.test.ts`, adapting tool names).
- `resolve_main_agent_policy` / `resolve_subagent_policy` reproduce the merge order: agent → global → provider → profile → explicit → also-allow.
- Existing `disable_main_computer` + `disable_delegate_gui` behavior preserved — flags become sugar layered on top of the new resolver.
- Level 2 VM smoke with a `deny: [delegate_gui]` config file produces the same trajectory shape as `DISABLE_DELEGATE_GUI=1`, confirming the sugar-layer equivalence.

## Status

Not started. Blocked on US-OC-052 landing + picking a config-file format.
