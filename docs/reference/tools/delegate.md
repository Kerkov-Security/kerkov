# `delegate` and `agent_wait`

`delegate` hands self-contained sub-tasks to isolated agents. `agent_wait`
collects their reports. Together they are how one conversation fans work out to
several without drowning in the output.

Delegate when the work branches — several investigation paths at once — or when a
sub-task would flood the main conversation with noise that only its conclusion
matters. A wide directory brute-force belongs in an agent; its one useful line
comes back, the ten thousand 404s do not.

Both are registered by the CLI, always. The internals — supervision, detachment,
focus, review, the shared workspace — are in the
[agent subsystem concept page](../../concepts/agent-subsystem.md); the operator
controls are in the [agents guide](../../guides/agents.md).

## `delegate`

Spawns a batch of agents and **returns immediately** with their ids. The agents run
detached in the background while the master keeps its turn.

### Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `tasks` | array of task specs | **required** | One or more tasks, launched concurrently. |

Each task spec:

| Field | Type | Default | Description |
|---|---|---|---|
| `agent_type` | string | **required** | Profile: a built-in (`recon`, `analysis`, `exploit`, `general`, `container-operator`) or a user-defined `AGENT.md`. |
| `instruction` | string | **required** | A clear, self-contained task. The agent does not share the master's conversation. |
| `name` | string | derived | A short kebab-case name (2–4 words) — what you see while it runs, e.g. `tomcat-enum`. Falls back to a slug of the instruction, then the profile. |
| `success_criteria` | string | — | How the agent knows it is done. Optional but recommended. |
| `allowed_tools` | array[string] | profile's set | A subset of the profile's tools. Can only **narrow**, never widen. `report` is always available and need not be listed. |

Batch size is capped at
[`AGENT_MAX_BATCH`](../configuration.md#agents) (5), with further limits on depth,
total, and concurrency.

### Self-contained is not optional

An agent starts cold. It does not see the master's conversation, only its
`instruction`, `success_criteria`, and the shared [ledger](ledger.md). An
instruction that says "continue the scan from before" gives the agent nothing to
continue from. Write it as if to someone who just walked in.

### Approval

The master gets approval **required** on a `delegate` batch, and it offers **no
broad rung** — each batch is a distinct grant of autonomy, so approving one never
carries to the next. `timeout_seconds`-style variance does not apply here; nothing
is saveable.

This gate matters because spawned agents *auto-approve* their own tool calls within
their profile's whitelist. Approving the batch is approving that autonomy for those
tasks. It is also why `sast_scan` is in no built-in profile — a whitelisted call
bypasses the prompt, and that scan is worth seeing.

### Returns

A confirmation naming each spawned agent and its task, with the `agent_wait` call
to collect them. Metadata carries `agent_ids`, `detached: true`, and an `agents`
list of `{id, name, profile, task}`.

Ids are internal (`a1`, `a2`, monotonic, never reused — what `agent_wait` and logs
key on). Names are what you chose and what the CLI shows. Model-facing text carries
both, as `name (id, profile)`.

### Example

```json
{
  "tasks": [
    {
      "agent_type": "recon",
      "name": "smb-enum",
      "instruction": "Enumerate SMB on 10.10.10.5: shares, null sessions, signing, users. Do not exploit.",
      "success_criteria": "A list of shares and whether null/guest access works."
    },
    {
      "agent_type": "recon",
      "name": "web-enum",
      "instruction": "Enumerate the web app on 10.10.10.5:8080: tech stack, directories, obvious version strings.",
      "success_criteria": "Tech stack identified and a directory listing."
    }
  ]
}
```

## `agent_wait`

Blocks on delegated agents and returns their reports.

### Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `ids` | array[string] | — | Agent ids to wait for, e.g. `["a1", "a2"]`. Omit to collect every report not yet seen. |
| `timeout_s` | number | — | Give up after this many seconds and return what is ready. Omit to wait for all. |

### You do not have to call it

If the master never calls `agent_wait`, finished reports are still delivered — each
front end drains completed reports at the next turn boundary and appends them as
messages, exactly once. `agent_wait` is the way to block *now*; letting reports
arrive on their own is the way to keep working meanwhile.

A `timeout_s` that expires with agents still running is a **success**, not an
error: it returns the ready reports plus a `Still running: …` line. Losing finished
work to a timeout would be the worse outcome.

### Approval

Auto-approved (`allow: "agent_wait({})"`). Approval widens to the whole tool — it
only blocks on agents already spawned and approved.

### Returns

The agents' reports, concatenated in one consistent shape whether they arrive
through `agent_wait` or at the turn boundary. Each report is headed with the
agent's name, profile, status, id, and token cost, followed by `FINDINGS:` and, if
present, `NEXT STEPS:`.

Agent status is one of:

| Status | Meaning |
|---|---|
| `completed` | The agent finished its task and reported. |
| `partial` | It made progress but did not finish — usually stopped, or out of iterations. Read `NEXT STEPS`. |
| `stuck` | It could not proceed. A batch containing a `stuck` agent surfaces an error alongside the reports. |

The report also prints the agent's **engagement id**. Pass that to
[`recall`](recall.md)'s `engagement` filter to read the raw tool output behind a
summary that was too thin.

### Example

```json
{ "ids": ["a1", "a2"] }
```

Collect whatever has finished, without blocking on the rest:

```json
{ "timeout_s": 0.1 }
```

## Configuration

| Variable | Default | Effect |
|---|---|---|
| [`AGENT_MAX_DEPTH`](../configuration.md#agents) | `2` | Deepest a delegation chain may nest. |
| [`AGENT_MAX_TOTAL`](../configuration.md#agents) | `20` | Total agents per session. |
| [`AGENT_MAX_BATCH`](../configuration.md#agents) | `5` | Agents one `delegate` may spawn at once. |
| [`AGENT_MAX_CONCURRENT`](../configuration.md#agents) | `5` | Live agents at the same time. |
| [`AGENT_MAX_ITERATIONS`](../configuration.md#agents) | `30` | Tool-call iterations an agent may run before stopping. |
| [`AGENT_TEMPERATURE`](../configuration.md#agents) | _(unset)_ | Sampling temperature for agents. |

## Related

- [Agents guide](../../guides/agents.md) — `/agents`, attaching, review, stop.
- [Agent subsystem](../../concepts/agent-subsystem.md) — how detachment,
  supervision, and delivery actually work.
- [Agent profiles](../agent-profiles.md) — the built-in profiles and the `AGENT.md`
  schema.
- [`recall`](recall.md) — reading the raw output behind a delegated report.
- [`ledger`](ledger.md) — the shared working memory agents read and write.
