# `ledger`

Writes confirmed facts into the findings ledger — the model's structured working
memory, re-shown to it at the top of context on every turn.

This is the answer to "the model forgot the credential it found forty messages
ago." The ledger lives *outside* the summarized history, so context compaction
never touches it: a credential recorded now is still in front of the model after
the conversation has been summarized three times. Anything not recorded here can
be summarized away.

Registered only when
[`KB_ENABLED=true`](../configuration.md#knowledge-base-and-findings-ledger) (the
default) — the same switch as [`recall`](recall.md), with no flag of its own. For
the operator's view, see the [memory guide](../../guides/memory.md).

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `op` | `add` \| `list` \| `remove` \| `graph` | **required** | Record, read, retract, or read the resource graph. |
| `section` | see below | — | Required for `add`; an optional filter for `list`. |
| `entry` | object | — | The record to store. Required for `add`. |
| `entry_id` | integer | — | Entry to retract. Required for `remove`; ids come from `list`. |
| `artifact_ref` | integer | — | Artifact id linking an entry to its evidence — the `N` from an `artifact#N` reference. Required for observed connections; connection references must be positive integer ids of existing artifacts. |
| `offset` | integer ≥ 0 | `0` | For `graph`, continue from the previous page's `next_offset`. Restart at `0` after inventory edits. |

There is **no `update` operation**. Use `add` with the same identity to enrich a
fact or replace a connection's assertion. To correct the identity itself through
the tool, remove the old entry and add the replacement.

## Sections and their identity fields

Nine sections. Each has identity fields (marked `*`) that an `add` must supply.

| Section | Holds | Fields |
|---|---|---|
| `assets` | Resources you are protecting | `kind*`, `value*`, `name` |
| `targets` | Subjects of an offensive assessment | `kind*`, `value*`, `name` |
| `connections` | Resource relationships and direct reachability | `source_id` or `source_vantage`, `target_id*`, `relation*`, `provenance*`, `note`; `reaches` also requires `protocol*`, `port*`, optional `observed_at` and `service` |
| `hosts` | Machines in scope | `ip*`, `hostname*` (at least one), `os`, `note` |
| `services` | Listening services | `host*` (or `ip*`), `port*`, `service`, `version`, `note` |
| `credentials` | Recovered credentials | `username*`, `host*` or `source*`, `secret`, `kind`, `validated` |
| `findings` | Confirmed weaknesses | `title*`, `severity`, `host`, `note` |
| `flags` | Captured flags | `value*`, `label`, `host` |
| `next_steps` | Planned work | `step*` |

For `assets` and `targets`, `kind` is one of `domain`, `ip`, `url`, or
`repository`, and the value is normalized and validated per kind: IP addresses are
canonicalized and reject zone identifiers, domains are IDNA-encoded and
lowercased, repository values must be absolute local paths (`~/path` is accepted
and expanded), and URL identity preserves path, query, and fragment case. Extra
fields are rejected rather than silently dropped, and `name` never affects
identity.

For `connections`, resource endpoints must be existing `assets` or `targets` entry
ids, obtained from `list`, `graph`, or a successful resource `add`. A `reaches`
source may instead be an explicit `source_vantage` label. IDs must be
positive integers within SQLite's signed 64-bit range. At the tool boundary,
integer-valued provider numbers such as `1.0` are normalized to integers because
Gemini's protobuf arguments represent JSON numbers as doubles. This applies to
`source_id`, `target_id`, connection `artifact_ref`, and `reaches.port`; strings, booleans,
fractional numbers, nonfinite numbers, and out-of-range values are rejected.
The storage model remains strict about integer IDs. Extra fields are rejected.
The supported directions are:

| Relation | Source kind | Target kind | Meaning |
|---|---|---|---|
| `resolves_to` | `domain` | `ip` | DNS resolution has been observed or explicitly declared. |
| `deploys_to` | `repository` | `domain`, `ip`, or `url` | The repository deploys to this resource. |
| `reaches` | `domain`, `ip`, or an explicit `source_vantage` | `domain` or `ip` | The named source can directly reach this transport and destination port. |

For `reaches`, provide exactly one `source_id` or `source_vantage`, a `protocol`
of `tcp` or `udp`, and an integer destination `port` from 1 through 65535.
`source_vantage` is a stable, explicit label for the actual execution environment,
such as `runner:assessment-a` or `container:scanner-a`; it has at most 200 characters.
It is not an inventory resource or a default "your machine" location. Keep labels
consistent across observations and distinct across execution environments. An
optional `service` label, such as `https` or `ssh`, has at most 80 characters.
Labels are trimmed, case-sensitive, and cannot contain control characters.
Resource-backed source and target identities must be different.

An observed `reaches` connection defaults `observed_at` to the evidence artifact
creation time. An explicit ISO8601 timestamp with timezone (for example
`2026-10-05T12:30:00Z`) is stored in UTC and cannot be later than the evidence.
Supply a real observation timestamp from the evidence or omit it; do not invent one. Declared connections cannot have
`observed_at`. `source_vantage`, `protocol`, `port`, `service`, and `observed_at`
are forbidden on `resolves_to` and `deploys_to`.

`provenance` is either `observed` or `declared`:

- **Observed:** supply a top-level `artifact_ref` pointing to the tool output
  that supports the connection. The artifact must exist in the same engagement
  knowledge base. A DNS lookup can establish `resolves_to`; deployment evidence
  can establish `deploys_to`. Evidence for `reaches` must establish direct
  transport reachability from the named source to that destination and port.
- **Declared:** supply a non-empty `note` describing the explicit user
  declaration or configuration. An evidence artifact is optional, but must exist
  if supplied.

Do not infer DNS or deployment relationships from similar resource names. A
URL's hostname alone proves only that the URL uses that hostname; it does not
prove a DNS address or deployment relationship.
Likewise, a URL, DNS result, deployment, or service listing alone cannot establish
reachability from another environment. Record only actual connection observations
or explicit configuration. A `reaches` arrow establishes neither successful
authentication nor permission to use a service. Adjacent arrows do not prove
forwarding, gateway access, or an end-to-end multi-hop route. This operation only
records facts; it does not run a connection test or configure access.

!!! note "Asset and target roles do not change permissions"
    Recording something as an asset rather than a target is bookkeeping, not
    enforcement. Tool approval is governed entirely by
    [approval rules](../../guides/approvals.md).

## Identity is required, not inferred

An `add` that cannot be identified is rejected with a message naming the missing
field. That looks strict, and it is deliberate: the failure mode of guessing is
silent in both directions.

A host recorded with neither an IP nor a hostname gets a content hash for a key,
which never merges with its own later enrichment — so you end up with three rows
for one machine, each holding a third of what is known. And services recorded
without a host would all collapse onto the same port, merging `80` on four
different machines into one row.

## Merging, and how identity firms up

`add` merges by identity: re-adding a host with more detail enriches the existing
row rather than creating a second one. This is the normal way an entry grows —
first the IP, later the OS, later a note.

Identity itself firms up over time, so each section also carries *alias* keys: the
hostname a host was first known by before its IP was confirmed, the `source` a
credential was recorded against before it was tied to a `host`, the credential
recorded before its `kind` was known. A lookup checks key *or* alias, folds
duplicates into the oldest row, and promotes that row to the canonical key — so
learning more about a fact never forks it.

For the fact sections, ownership and evidence merge as follows:

- `engagement_id` — the first contributor keeps ownership. Cleanup deletes by
  owner, so letting an enriching agent take ownership would let its cleanup delete
  the master's facts.
- `artifact_ref` — a supplied reference replaces the existing evidence link;
  a later detail-only add without a reference preserves it.

Resources and connections belong to the root engagement, including those added
by a delegated agent. Connections deduplicate by normalized resource identity
and relation, so referring to the same resource's asset or target entry updates
the same connection. For `reaches`, identity additionally includes the explicit
source identity, transport protocol, and destination port. TCP and UDP, different
ports, reverse directions, and different source environments remain separate.
Service labels and observation timestamps describe evidence and do not alter identity.
Re-adding an observed connection replaces its note and
evidence with the new validated observation; it does not accumulate observations.
For `reaches`, an older `observed_at` cannot overwrite a newer observation, even
when submitted later. The tool reports `outcome: stale_observation_retained`,
and retains the newer record unchanged. Equal observation times may replace
metadata and evidence. Removing an observation is the way to retract it.
A later declaration cannot downgrade an observed connection: its observed record
and evidence stay intact. The tool explicitly reports that the declaration and its
note were not stored, with `outcome: observation_retained` in metadata.
Re-adding a declared connection replaces its declaration,
and an observation can promote it to observed. To intentionally replace an
observed connection with a declaration, remove the connection first.
Retract a connection by its connection entry id when it is no longer true.

Connection cleanup follows resource identity:

- Changing a name or retaining a resource under another role preserves its
  connections. If equivalent entries merge, connections follow the surviving
  resource entry.
- Changing the resource's kind or value retracts its old connections, unless
  another asset or target entry still represents the old identity.
- Removing the last asset/target entry for a resource removes its incident
  connections. Deleting an evidence artifact retracts observed connections
  supported by it; declared connections remain with their evidence link cleared.

## Resource graph

`op=graph` returns a compact JSON page with `nodes`, `edges`, and `next_offset` from
one consistent ledger snapshot. Nodes represent resources by their normalized kind and value.
Each explicit reachability source also produces a `kind: vantage` node whose
`value` is its label and whose `roles` and `entry_ids` are empty. Vantage nodes
disappear when their last connection is retracted; no resource records are created.
A resource recorded as both an asset and a target becomes one node carrying both
roles. Credentials, findings, and the other fact sections are not graph nodes.

Each page contains at most 5,000 JSON characters and remains intact in model
history, including when generic output limits are lower. Follow a non-null
`next_offset` with `{"op":"graph","offset":<next_offset>}` until it is null.
Offsets count nodes first, then edges; an edge's endpoint may be on an earlier
page. Each call reads a fresh snapshot, so restart at `0` after inventory edits.
Metadata counts describe the whole graph, not just the current page.

If a single resource's URL, path, or name is too large to fit, the page keeps its
kind, roles, and entry IDs but omits its `value` and `names`, marking those fields
in `omitted_fields`. Use `list` for the full record. The stored identity and the
application graph are unchanged.

| Object | Fields |
|---|---|
| Node | `kind`, `value`, `roles` (`asset`/`target`), `entry_ids`, and optional `names` keyed by role. |
| Edge | `source_id` or `source_vantage`, `target_id`, `relation`, `connection_ids`, `provenances`, and optional `artifact_refs`. Reachability includes `protocol`, `port`, and optional `service` and `observed_at`. |

To add a connection, use a node's integer `entry_ids` as `source_id` or
`target_id`; to retract one, use the edge's `connection_ids` with `remove`.
Each resource endpoint's `source_id` and `target_id` is the first `entry_ids` value
of its node, so these are usable ledger IDs too. A vantage-backed edge instead
has `source_vantage` containing the source label, with no `source_id`.
Hashes and full evidence
notes are omitted from tool output to keep small graphs below the default output
clipping threshold. Read full connection notes with `op=list` and
`section=connections`; retrieve evidence with `recall` using an `artifact_refs` ID.

Edges include recorded `resolves_to`, `deploys_to`, and `reaches` connections with their
provenance and evidence. The graph also derives `uses_host` edges from URL
resources to existing domain or IP resources whose identity exactly matches the
URL hostname. These edges have derived provenance; they perform no DNS lookup,
create no ledger entries, and cannot be added or removed through the tool. Edit
or remove the underlying resource to change a derived edge.

Graph projection is read-only. Application code can use `FindingsLedger.graph()`
for the full graph with stable identity-based node and edge `id` hashes, edge
`source`/`target` references to those node IDs, role-specific node `records`, and
full edge `evidence` records containing `connection_id`, `provenance`,
`artifact_ref`, and `note`. Reachability evidence also retains any `service` and
`observed_at`; the edge surfaces the latest observation's metadata. Virtual vantage
nodes have deterministic `vantage:` IDs and empty `records`. Existing resource
and legacy relation shapes remain unchanged. Artifact bodies are excluded.
The API supplies inventory and direct reachability data to the host-grouped
browser network map. It displays `vantage` origins and recorded `reaches` routes
with transport, service and evidence details.

## Injection and trimming

The ledger is serialized and inserted as a system message on every turn, under the
header `=== FINDINGS LEDGER (working memory — kept verbatim) ===`. It is never
written into the conversation history, so compaction never sees it and no
exclusion logic is needed.

Where it lands depends on the provider. Providers that cache a stable prompt prefix
— currently Anthropic and DeepSeek — get the ledger as the **final** message, after
the stable history, so a ledger update does not invalidate the cached
system-plus-tools-plus-history prefix. Every other provider gets it high, right
after the base prompt. The tradeoff is head-position salience against recency
salience; the cost of the alternative is re-billing the whole prefix on every turn.

Because the block sits outside compaction, it cannot be reclaimed once it grows —
so serialization trims to 16,000 bytes (roughly 4k tokens) and appends a count of
what it omitted. Trimming drops **individual entries**, not whole sections: entries
are ordered by section priority, then least-recently-touched first, and that order is
walked greedily keeping whatever still fits. The priority order is credentials,
flags, findings, next steps, assets and targets, connections, hosts, services, so
**enumeration gives way before conclusions** — but a section can be partially
retained, and one oversized high-priority entry can be skipped while smaller
lower-priority entries after it are kept.

Nothing is deleted by trimming. `op=list` still reads everything.

## Approval

Auto-approved by the packaged rules:

```yaml
- allow: "ledger({})"
```

Approval widens to **the whole tool** — every call writes only to local engagement
state. Nothing is executed, nothing is sent anywhere.

## Returns

| Op | Output | Metadata |
|---|---|---|
| `add` | `Recorded to <section> ledger as entry #<id>.`, or a notice when an observation prevents storing a declaration or an older route observation. | `entry_id`, `section`; `outcome: observation_retained` for an ignored declaration or `stale_observation_retained` for an older observation |
| `list` | One line per entry: `#<id> [<section>] <entry>`, plus `(artifact#N)` where an evidence link exists. Empty ledger says so. | `count` |
| `remove` | `Removed ledger entry #<id>.` | `entry_id` |
| `graph` | Bounded JSON page with `nodes`, `edges`, and `next_offset` (null on the last page); both arrays are empty when there are no resources. | `node_count`, `edge_count` (whole graph) |

A `remove` of an id that does not exist fails. `list` on an empty ledger succeeds
with `count: 0` — nothing recorded yet is an answer, not an error.

If no knowledge base is active for the session, every op fails saying so.

## Examples

Record a credential with where it works:

```json
{
  "op": "add",
  "section": "credentials",
  "entry": {
    "username": "svc_backup",
    "host": "10.10.10.5",
    "secret": "Backup2024!",
    "kind": "smb",
    "validated": true
  }
}
```

A finding, linked to the artifact that proves it:

```json
{
  "op": "add",
  "section": "findings",
  "entry": {
    "title": "Unauthenticated Solr admin on 8983",
    "severity": "high",
    "host": "10.10.10.5"
  },
  "artifact_ref": 42
}
```

Enrich a host already recorded by hostname — this merges, it does not duplicate:

```json
{
  "op": "add",
  "section": "hosts",
  "entry": { "hostname": "dc01.corp.local", "ip": "10.10.10.5", "os": "Windows Server 2019" }
}
```

Scope a target:

```json
{ "op": "add", "section": "targets", "entry": { "kind": "url", "value": "https://target.example/app" } }
```

Record resources before connecting them:

```json
{ "op": "add", "section": "assets", "entry": { "kind": "domain", "value": "api.example.com" } }
```
```json
{ "op": "add", "section": "targets", "entry": { "kind": "ip", "value": "192.0.2.10" } }
```

Use the actual returned entry ids. If the domain is `#12`, the IP is `#13`, and
`artifact#42` contains the DNS observation:

```json
{
  "op": "add",
  "section": "connections",
  "entry": {
    "source_id": 12,
    "target_id": 13,
    "relation": "resolves_to",
    "provenance": "observed"
  },
  "artifact_ref": 42
}
```

For an explicit deployment declaration, first record the repository:

```json
{ "op": "add", "section": "assets", "entry": { "kind": "repository", "value": "/work/api" } }
```

If it returns `#15`, record what the user declared:

```json
{
  "op": "add",
  "section": "connections",
  "entry": {
    "source_id": 15,
    "target_id": 12,
    "relation": "deploys_to",
    "provenance": "declared",
    "note": "The user confirmed that /work/api deploys to api.example.com."
  }
}
```

For a directly observed TCP connection from a known execution environment to
domain `#12`, record the real observation time and evidence. This example assumes
`artifact#43` contains that connection observation from `runner:assessment-a`:

```json
{
  "op": "add",
  "section": "connections",
  "entry": {
    "source_vantage": "runner:assessment-a",
    "target_id": 12,
    "relation": "reaches",
    "protocol": "tcp",
    "port": 443,
    "service": "https",
    "provenance": "observed",
    "observed_at": "2026-10-05T12:30:00Z"
  },
  "artifact_ref": 43
}
```

For a configured connection, use `provenance: declared` with a note explaining
the explicit configuration, and omit `observed_at`. When the actual source is
already represented by a domain or IP inventory record, replace `source_vantage`
with its `source_id`.

Read the recorded connections or the combined graph:

```json
{ "op": "list", "section": "connections" }
```
```json
{ "op": "graph" }
```

Read one section, then retract a fact that turned out wrong:

```json
{ "op": "list", "section": "credentials" }
```
```json
{ "op": "remove", "entry_id": 7 }
```

## Storage and privacy

Stored in the same database as [`recall`](recall.md) —
`~/.kerkov/engagements/<root-engagement-id>.kb.sqlite`, table `ledger_entries` —
so it inherits the same root-keying, concurrency settings, and lifecycle. A
database created before the ledger existed migrates in place, keeping its
artifacts.

Tree-shared like the knowledge base: delegated agents both write to the ledger and
receive the injected copy. Because tools pass by reference, an agent's writes are
recorded under the master's engagement id — cosmetic, since the row is tree-shared
either way.

!!! warning "Secrets are stored and re-injected unmasked"
    A recorded credential is kept in cleartext and re-shown to the model in full on
    every turn. That is the point — a masked credential is not usable — but it means
    the database and the provider conversation both contain it. Storage is local and
    Kerkov adds no telemetry, but the injected block is part of every provider
    request, so against a hosted provider every turn sends the ledger in cleartext.

## Related

- [`recall`](recall.md) — the evidence archive. The ledger holds the conclusion;
  `recall` holds the tool output behind it. Link the two with `artifact_ref`.
- [Memory guide](../../guides/memory.md) — what survives compaction, operator-side.
- [`/asset` and `/target`](../slash-commands.md#engagements) — writing the resource
  sections yourself, without going through the model.

Observed reachability defaults `observed_at` to the evidence artifact's server-side
creation time. An explicit observation time must not be later than that artifact.
Use `source_id` for IP addresses and existing domain resources; `source_vantage`
is an execution-environment label. Record reachability when the source matters
(pivots or non-default vantages), and keep ordinary port scan results in services
instead of duplicating each service as a reachability record.
