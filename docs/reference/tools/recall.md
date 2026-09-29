# `recall`

Searches the engagement knowledge base: the full output of every tool run so far,
including runs by delegated agents.

The point is that the conversation and the archive are not the same thing. A large
scan result is replaced in the conversation with a short `artifact#N` handle, and
compaction later evicts the rest — but the original output is stored intact.
`recall` is how it comes back.

Reach for it before re-running a scan or re-fetching a page. If it has been done
once in this engagement, the output is already here, in full, and retrieving it
costs a fraction of doing the work again.

The archive stores each tool's **returned output**. Scanner report files and other
files named in a result are not automatically indexed. For a tool that returns a
bounded summary, `recall` retrieves that summary; use [`read`](read-write.md) on the
report path, or filter the file with [`exec`](exec-process.md), for omitted detail.

Registered only when [`KB_ENABLED=true`](../configuration.md#knowledge-base-and-findings-ledger)
(the default). For the concepts behind the store, see the
[memory guide](../../guides/memory.md).

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `query` | string | **required** | Plain words or keywords. `artifact#42` fetches that artifact directly. |
| `kind` | `tool_output` \| `file` \| `http_response` | — | Filter by artifact kind. |
| `source` | string | — | Substring matched against the producing tool name **and** the artifact title. |
| `engagement` | string | — | Restrict to one engagement id. Omit to search this engagement and every agent spawned from it. |
| `limit` | integer | `5` | Maximum results, capped at 20. |
| `full` | boolean | `false` | Return each match's complete stored body instead of a snippet. |

### `source` matches the target too

`source` is checked against both the tool name and the artifact's title, and the
title is where the target lands — `http: https://10.10.10.5/admin`,
`read: /etc/passwd`. The stored source column only ever holds the tool name, so
matching that alone would make `source: "10.10.10.5"` return nothing while looking
like a working filter.

So all of these work: `source: "exec"`, `source: "http"`, `source: "10.10.10.5"`,
`source: "/etc/passwd"`.

### Artifact kinds

Kinds are assigned from the producing tool, not guessed from content:

| Kind | Produced by |
|---|---|
| `file` | `read` |
| `http_response` | `http` |
| `tool_output` | everything else |

There are only three, deliberately. A kind nothing can produce would be a filter
that returns confidently empty results forever. Credentials and notes are
*deliberately absent* — those facts belong in the [findings ledger](ledger.md),
which has its own sections for them, not in an artifact kind.

## Snippet first, then expand

Two stages, for the same reason [`web_search`](web-search.md) and
[`read_url`](web-search.md#read_url) are two tools:

1. A plain query returns ranked **snippets** — 400 characters each — with a header
   naming the artifact, its title, the tool behind it, its kind, size, and which
   engagement produced it.
2. Once a snippet shows the right record, expand it: `recall(query='artifact#42')`
   returns the full body, up to 30,000 characters.

`full=true` expands every hit at once. It is capped at 60,000 characters across
the whole result; hits past the budget come back as headers with a note to expand
them individually. Prefer expanding one artifact by handle — `full=true` with
`limit: 20` is a way to spend a large amount of context on records you have not
looked at yet.

`recall` is the one tool exempt from output normalization, so an expansion
deliberately returns the whole body rather than a trimmed preview. That is also why
an expansion can pin the recent-messages window during compaction — it is a large
message by design.

## Tree-wide by default

One store per engagement *tree*, not per engagement id. A spawned agent gets its
own engagement id but writes into its master's store, so `recall` with no
`engagement` filter searches the master and every agent under it.

This is how you get behind a delegated agent's summary. The agent's report prints
its engagement id; pass that id as `engagement` and you are reading the raw tool
output its FINDINGS were condensed from.

## Approval

Auto-approved by the packaged rules:

```yaml
- allow: "recall({})"
```

Approval widens to **the whole tool** — every call reads this engagement's own
artifacts, which the session produced itself.

## Returns

Matching records as output, each with a header line:

```text
— artifact#42 | exec: nmap -sV -p- 10.10.10.5 | via exec | [tool_output, 18324 B, this session: eng-20260927-1a2b]
```

The origin field reads `this session` for the master's own artifacts, or the
agent's profile name for one an agent produced.

Metadata carries `count` and `hits` — each with `artifact_id`, `engagement_id`,
`kind`, `source`, `title`, `size_bytes`, `agent_type`, and the artifact's own stored
metadata.

An empty result is a **success**, and it explains itself rather than just saying
nothing matched:

- Filtered to an engagement with no artifacts → says so, and lists the engagement
  ids that do have artifacts. An agent that is still running has not stored
  anything yet.
- Nothing matched in a scope that does hold artifacts → says how many are stored,
  so a bad query is distinguishable from an empty store.

Two things fail rather than returning empty: no knowledge base active for the
session, and an `artifact#N` that does not exist.

## Examples

Find something without knowing what produced it:

```json
{ "query": "port 8080 open" }
```

Expand the record a snippet identified:

```json
{ "query": "artifact#42" }
```

Only pages fetched from one host:

```json
{ "query": "admin panel", "kind": "http_response", "source": "10.10.10.5" }
```

Read the raw output behind a delegated agent's report:

```json
{ "query": "smb signing", "engagement": "eng-20260927-9f3c", "limit": 10 }
```

## Storage and privacy

The store is a SQLite database with an FTS5 index at
`~/.kerkov/engagements/<root-engagement-id>.kb.sqlite`, created on first write.

Search is local full-text. Nothing leaves the machine, and there is no embedding
service in the default path — semantic search is opt-in via
[`KB_EMBEDDINGS`](../configuration.md#knowledge-base-and-findings-ledger) and inert
until a backend ships.

!!! warning "Plaintext on local disk"
    The store holds whatever the tools returned, harvested credentials included.
    Nothing is redacted and nothing is encrypted, and Kerkov adds no telemetry —
    but a `recall` result is returned into model history, so expanding an artifact
    sends its contents to whatever provider is configured. Treat the file, and what
    you recall from it, with the same care as the engagement it documents.

## Related

- [`ledger`](ledger.md) — confirmed facts, always in context. `recall` is the
  evidence behind them; the ledger is the conclusion.
- [Memory guide](../../guides/memory.md) — what survives compaction, from the
  operator's side.
- [`/engagements`](../slash-commands.md#engagements) — the engagement ids to pass as
  `engagement`, including agent sub-runs.
