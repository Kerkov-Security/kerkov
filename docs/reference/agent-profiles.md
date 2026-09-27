# Agent profiles

An **agent profile** is the reusable definition of an agent: a name, a
description the master reads when deciding what to delegate, a system prompt the
agent runs under, and — most importantly — the maximum set of tools that agent may
use. Kerkov ships five built-in profiles, and you can add your own as `AGENT.md`
files.

For how agents run, see [`delegate` and `agent_wait`](tools/delegate.md) and the
[agent subsystem concept page](../concepts/agent-subsystem.md).

## Built-in profiles

| Profile | For | Tools |
|---|---|---|
| `recon` | Enumeration, OSINT, and scanning — no exploitation, no write. | `read`, `http`, `exec`, `process`, `sbom_generate`, `vulnerability_scan`, `skill_lookup`, `recall`, `ledger` |
| `analysis` | Read-only review of logs, code, or artifacts. | `read`, `skill_lookup`, `recall`, `ledger` |
| `exploit` | Exploitation and payload work on authorized targets. | `read`, `write`, `http`, `exec`, `process`, `skill_lookup`, `recall`, `ledger`, `zap` |
| `general` | Default ad-hoc delegation. | `read`, `http`, `exec`, `process`, `skill_lookup`, `recall`, `ledger` |
| `container-operator` | Low-token Docker Compose startup, readiness, status, and shutdown. | `read`, `exec`, `container_lifecycle`, `skill_lookup`, `recall`, `ledger` |

Two things every profile has that are not in the table: the `report` tool (how any
agent finishes — always present, never listed) and, below the depth cap,
`delegate` (so an agent can fan out further).

Notably absent from every built-in whitelist: [`sast_scan`](tools/sast.md). A
whitelisted call bypasses interactive approval, and a wide OpenGrep scan is
expensive enough that the operator should see it first. A user-defined profile can
opt into it.

## The `AGENT.md` schema

A user-defined profile is a Markdown file with YAML frontmatter, saved at
`agents/<name>/AGENT.md` in the project root. The frontmatter declares the profile;
the body is the system prompt.

```markdown
---
name: example-recon
description: Example user-defined recon agent — narrow tools, OSINT-focused.
allowed_tools:
  - read
  - http
  - skill_lookup
max_iterations: 20
---

You are a focused open-source reconnaissance agent.

Your job is to enumerate publicly-available information about the target the
master delegates to you. You have no execution or write capability — read, HTTP
fetch, and skill lookup only.

Stay on-task. When done (or when you cannot make progress), call the `report` tool
with status="completed", "partial", or "stuck". Do NOT emit further tool calls
after `report`.
```

### Fields

| Field | Required | Default | Description |
|---|---|---|---|
| `name` | ✓ | — | Unique profile name. The `agent_type` passed to `delegate`. |
| `description` | ✓ | — | One line shown to the master LLM in its profile index. This is what the master reads to decide when to use the profile, so make it discriminating. |
| `allowed_tools` | ✓ | — | A list of tool names. The **maximum** whitelist for any spawn of this profile — for that agent's own calls; see [the scope of the ceiling](#the-ceiling-is-per-agent-not-per-subtree). |
| `max_iterations` | — | `30` | Per-agent loop cap. Also bounded by [`AGENT_MAX_ITERATIONS`](configuration.md#agents); the effective cap is the lower of the two. |
| `system_prompt` | — | the body | The agent's system prompt. If omitted, the Markdown body after the frontmatter is used — which is the normal way to write it. |

The system prompt comes from `system_prompt` if set, otherwise the body; a profile
with neither is skipped. `report` is always available and never listed in
`allowed_tools`.

### `allowed_tools` is a ceiling

The whitelist is the *maximum* set a spawn may use. `delegate` can request a subset
via its own `allowed_tools` field, which **narrows** the profile's set — it can
never widen it. Requesting a tool outside the profile's whitelist rejects the
**entire batch** before any agent starts. For example, requesting `["read", "exec"]`
for `analysis` fails because that profile does not allow `exec`.

`report` is accepted in the requested list as a special case, since every agent
has it. An explicit subset must still retain at least one of the profile's tools;
an empty list or `["report"]` alone is rejected. Successful requests receive the
selected profile tools, plus `report` and the depth-dependent `delegate` described
above.

Tool names must match the registered names exactly — see the [tools
reference](tools/index.md). `recall` and `ledger` follow the same rule as any other
tool: every built-in profile whitelists them so agents can read what the engagement
already gathered, but a user-defined profile that omits them does not get them.

### The ceiling is per agent, not per subtree

The whitelist bounds the tools *this* agent may call. It does not bound the agents it
spawns.

`delegate` is appended by the runner after `allowed_tools` has been applied, so it is
outside the whitelist by construction — which is also why naming it in a `delegate`
request *fails*: validation checks the request against the profile's list, and
`delegate` is in no profile's list. (`report` is the one name excused there, since it
is the tool an agent must call to finish.) Any agent below
[`AGENT_MAX_DEPTH`](configuration.md#agents) has it, and a nested delegation resolves
its own `agent_type` against the full profile index and draws from the same master
tool pool. Nothing requires the child's profile to be a subset of the parent's: an
`analysis` agent restricted to `read` can spawn an `exploit` child holding `write`,
`exec`, and `zap`. Because an agent auto-approves any call inside its own whitelist,
that child's `exec` never reaches an approval prompt — the operator approved the
master's `delegate`, one level up.

Treat `allowed_tools` as a capability list for one agent, then bound the tree
separately:

- `AGENT_MAX_DEPTH=1` withholds `delegate` from every agent, which makes the
  approved whitelist the whole of what a delegation can reach.
- [`/agents review <name>`](../guides/agents.md#reviewing-an-agents-tool-calls) puts
  one agent's calls back through the operator's approval path, but only while you are
  attached to it.

## Loading and precedence

Profiles are loaded at startup:

- The five built-ins are always present.
- Every `agents/<name>/AGENT.md` under the project root is scanned and parsed.

A file is **skipped with a logged warning**, not fatally rejected, when it is
missing a required field, has no valid frontmatter, has an empty system prompt, is
larger than 256 KB, or fails validation. One bad profile never stops the others
loading.

A user-defined profile may **shadow a built-in** of the same name — a warning is
logged, and the user-defined one wins, because writing the file is opting into
that. Two user-defined profiles with the same name: the last one loaded wins, also
with a warning.

The master does not see full profiles — it sees a compact index of names,
descriptions, and tool sets, truncated past 8 KB so a large `agents/` directory
cannot crowd out the master's own context.

## Related

- [`delegate` and `agent_wait`](tools/delegate.md) — spawning agents against these
  profiles and collecting their reports.
- [Agents guide](../guides/agents.md) — watching, talking to, reviewing, and
  stopping a running agent.
- [Agent subsystem](../concepts/agent-subsystem.md) — the internals.
- [Tools reference](tools/index.md) — the tool names `allowed_tools` draws from.
