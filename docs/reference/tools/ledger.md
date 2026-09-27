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
| `op` | `add` \| `list` \| `remove` | **required** | — |
| `section` | see below | — | Required for `add`; an optional filter for `list`. |
| `entry` | object | — | The record to store. Required for `add`. |
| `entry_id` | integer | — | Entry to retract. Required for `remove`; ids come from `list`. |
| `artifact_ref` | integer | — | Artifact id linking an entry to its evidence — the `N` from an `artifact#N` reference. |

There is **no `update` operation**. Correcting an entry is `remove` then `add`; an
`add` that merges is how enrichment normally happens, so `update` had no job left
to do.

## Sections and their identity fields

Eight sections. Each has identity fields (marked `*`) that an `add` must supply.

| Section | Holds | Fields |
|---|---|---|
| `assets` | Resources you are protecting | `kind*`, `value*`, `name` |
| `targets` | Subjects of an offensive assessment | `kind*`, `value*`, `name` |
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

`engagement_id` is **insert-only** on merge: the first contributor keeps ownership.
Cleanup deletes by owner, so letting an enriching agent take ownership would let its
cleanup delete the master's facts.

`artifact_ref` is not insert-only. A merge keeps the existing evidence link only when
the incoming `add` omits one; an `add` that supplies its own `artifact_ref` replaces
it. A detail-only add never clears the link, but the most recent `add` to name an
artifact owns the reference.

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
flags, findings, next steps, assets and targets, hosts, services, so
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
| `add` | `Recorded to <section> ledger as entry #<id>.` | `entry_id`, `section` |
| `list` | One line per entry: `#<id> [<section>] <entry>`, plus `(artifact#N)` where an evidence link exists. Empty ledger says so. | `count` |
| `remove` | `Removed ledger entry #<id>.` | `entry_id` |

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
