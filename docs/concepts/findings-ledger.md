# Findings ledger

!!! note "Contributor-facing"
    How the ledger stores confirmed facts, merges them by identity, and stays in
    front of the model across compaction. The tool is documented in
    [`ledger`](../reference/tools/ledger.md); the operator's view is the
    [memory guide](../guides/memory.md).

The findings ledger is the model's structured working memory: a small store of
confirmed facts (`assets`, `targets`, `connections`, `containers`, `hosts`, `services`,
`credentials`, `findings`, `flags`, `next_steps`) that is **re-injected at the
top of context every turn**. It is the fix for "the model forgot the credential
it found forty messages ago" — because it lives *outside* the summarized history,
[compaction](context-and-compaction.md)
never touches it, and a fact recorded now is still verbatim in front of the model
after the conversation has been summarized three times.

Two layers implement it: `EngagementKnowledgeBase` owns the SQL, and `FindingsLedger`
(`src/kerkov/core/knowledge_base/findings_ledger.py`) owns the domain logic —
identity, merge, and serialization.

## It shares the knowledge base's file

The ledger lives in the **same `.kb.sqlite`** as [`recall`](knowledge-base.md), in a
`ledger_entries` table added in schema v2. An existing v1 database migrates in place,
keeping its artifacts. So the ledger inherits the knowledge base's root-keying, its
WAL/`busy_timeout` concurrency, and its lifecycle — a root delete takes the ledger
with it; an agent sub-run delete drops just that agent's ledger rows.

It is gated on the same `KB_ENABLED` switch — there is no separate flag.

## Identity is required, not inferred

Every section has identity fields that an `add` must supply (`identity_error`
rejects a call that cannot be identified). This looks strict; it prevents a silent
failure in both directions:

- A host recorded with neither IP nor hostname gets a content-hash key that never
  merges with its own later enrichment — so one machine becomes three rows, each
  holding a fraction of what is known.
- Services recorded without a host would all collapse onto the same port, merging
  `80` on four different machines into one row.

The required fields per section are in the [`ledger` reference](../reference/tools/ledger.md#sections-and-their-identity-fields).
Asset and target values are additionally validated and normalized per `kind`
(IP canonicalization, IDNA domains, absolute repository paths, case-preserving URLs)
by the shared `Resource` model.

## Merge, and how identity firms up

`add` merges by identity: re-adding a host with more detail enriches the existing
row instead of forking it. This is the normal growth path — IP first, OS later, a
note later still.

Identity itself firms up over time, so `_identity` returns not just a canonical
`dedup_key` but *alias* keys: the hostname a host was first known by before its IP,
the `source` a credential was recorded against before a `host`, the credential
recorded before its `kind`. `add_ledger_entry` looks a row up under key *or* alias,
folds duplicates into the oldest matching row, and promotes it to the canonical key.
So learning more about a fact never creates a second copy of it.

SSH credentials with an explicit `port` keep that port in their identity. Two
containers published on different ports of the same host therefore retain
separate access records. An old credential without a port is not assumed to be
the same endpoint. SSH records can also carry `private_key_file`,
`known_hosts_file` and `sudo`: these references survive compaction and let the
Compliance GUI retrieve known access. The GUI projects file references for an
exact host match, never the credential's raw `secret` or `password`. Container
names and deployment/DNS relationships do not establish a credential binding.

`engagement_id` is **insert-only** on merge: the oldest matching row wins, and the
first contributor keeps ownership. Cleanup deletes ledger rows by owner, so letting
an enriching agent take ownership would let *its* cleanup delete the master's facts.

`artifact_ref` is *not* insert-only, and the difference matters if you are relying on
it. A merge keeps the existing evidence link only when the incoming `add` omits one;
an `add` that supplies its own `artifact_ref` replaces whatever was there. So a
detail-only enrichment never clears an evidence link — but the last `add` to name an
artifact owns the reference, and a row's evidence attribution does not necessarily
point at the artifact that first established the fact.

Resource sections are a special case: an asset or target belongs to the engagement
even when an agent adds it, so `add` forces its `engagement_id` to the root.

## Container observations

The `containers` section holds last-known Docker Compose observations written by
[`container_lifecycle`](../reference/tools/container.md). Identity combines the
Compose directory, project name and container name, independently of shared IP
endpoints. Observations belong to the root engagement and replace their whole
snapshot, so cleared port mappings or health fields do not survive a refresh.

A successful shutdown marks the project's observations stopped. Missing
containers are marked removed only after a complete snapshot was actually read;
a failed inspection preserves the previous state. Published ports and HTTP probe
targets provide endpoint context without asserting network reachability.

The injected summary includes name, service, state, health, readiness and compact
port mappings. Full timestamps, readiness diagnostics, paths and endpoint IDs
remain available through `ledger op=list`.

## Connections, and the graph projection

The `connections` section records a relationship between two resources, and it
records it by their ledger ids (`source_id`, `target_id`), never by restating the
resource. There are three relations with fixed endpoint kinds: `resolves_to`
(a `domain` to an `ip`) and `deploys_to` (a `repository` to a `url`, `domain`, or
`ip`), plus `reaches` (a `domain`/`ip` or explicit execution environment to a
`domain`/`ip`). A reachability source uses exactly one existing `source_id` or
bounded `source_vantage` label. `reaches` requires transport `protocol` (`tcp` or
`udp`) and destination `port` (1–65535), with an optional `service` label.
The write validates resource endpoints' existence and their kinds in the same
transaction as the assertion, so a connection cannot name a resource that is not
there or a pairing the relation does not allow.

A connection's `provenance` is what makes it trustworthy. An `observed` connection
**requires an `artifact_ref`** — the evidence that establishes it — and a
`declared` connection requires a `note` describing the explicit declaration. The
rule the design enforces is that relationships are never guessed from naming
similarity: a URL whose hostname matches a domain is not, on that basis, a DNS
record. Observed outranks declared — a later declaration cannot downgrade an
observed connection, and the tool reports `observation_retained` rather than
silently discarding the attempt.

Observed reachability additionally requires the actual `observed_at` timestamp,
normalized to UTC from timezone-aware ISO8601. Declarations cannot carry an
observation timestamp, and writes never manufacture one. Reachability identity
includes the source identity, target identity, protocol, and port; a late write
of an older observation cannot replace newer evidence. This rule also applies
when resource roles merge and historical duplicate connections collide. Equal
observation times permit metadata replacement. The tool reports
`stale_observation_retained` when it keeps a newer observation.

The relation asserts only direct transport reachability from that specific
source. It does not establish authentication, authorization, forwarding, or an
end-to-end multi-hop route. URL ownership, DNS results, and deployment links do
not establish reachability. A source environment must be named explicitly and
consistently; there is no inferred runner identity or automatic probing.

`op=graph` is a **read-only projection** of resources and container observations, not a stored
structure. It folds an identity recorded as both an asset and a target into one
node, emits the recorded connections as edges, and additionally *derives*
`uses_host` edges where a URL's host exactly matches a `domain` or `ip` resource —
derived edges perform no DNS lookup, create no rows, and cannot be added or removed
through the tool. Containers appear as distinct `kind: container` nodes with
saved endpoint references. Their `entry_ids` identify observations and cannot be
used as connection endpoints; publishing a port never creates a reachability edge.
An observed connection whose evidence reference is missing or
invalid is left out of the projection entirely. Because the graph can outgrow a
single tool result, it paginates as compact JSON under a 5,000-character page
budget: follow `next_offset` until it is null. Each call reads a fresh snapshot, so
**a stale offset is meaningless after any edit** — restart at `0`. The full
hash-addressed graph, with evidence records, is available to application code
through `FindingsLedger.graph()`; the tool returns actionable ledger ids and
explicit vantage labels. A vantage-backed route creates a deterministic virtual
`kind: vantage` node with empty `roles`, `entry_ids`, and `records`; compact tool
edges use `source_vantage` instead of `source_id`. The virtual node disappears
when its last route is removed. Reachability edges and evidence preserve the
service label and actual observation time, keeping metadata from the freshest
observation when projecting historical duplicates. Removing evidence retracts
its observed routes, and endpoint deletion or editing follows the same resource
identity lifecycle as legacy relations. This is an additive JSON contract and
does not require a database schema migration.
The field-level contract is in the [`ledger` reference](../reference/tools/ledger.md#resource-graph).

## Resource profiles

`FindingsLedger.profile(entry_id)` is another read-only projection of the shared
ledger. It reads resource, host, service, finding, connection, and container
sections in one snapshot, then assembles the selected asset or target's context,
exact-address observations, incident graph connections, evidence references, and
current-record activity. It does not create a profile table or change stored data.
The GUI and the `ledger` tool's `op=profile` use this same projection.

Asset and target entries with the same normalized kind/value share an
identity-based resource ID and profile. Each role keeps its own editable name,
environment, tags, and notes. Host records match their explicit `ip` or `hostname`;
service and finding records match `host` or `ip`. Domain and IP normalization is
preserved, while URL paths and repository paths match with their original case.
A related resource is a navigation link, not a reason to inherit its findings:
DNS, deployment, and URL-host edges do not propagate observations into a profile.

Credential and flag sections, raw conversation data, and artifact bodies are
excluded. The projection returns evidence references; the web adapter separately
checks that a requested artifact is linked to a displayed record or connection
before and after reading its bounded preview. Section limits retain full counts
and identify clipping. `last_recorded_at` describes ledger updates, while
`last_observed_at` comes only from explicitly timestamped connection observations.
Activity describes the current records and their creation/update times, rather
than earlier versions or a complete scan history.

See the [profile contract](../reference/tools/ledger.md#resource-profiles) for
fields and limits, or the [web console guide](../guides/web-console.md#resource-profiles)
for opening profiles and preparing an investigation draft.

## No `update` in v1

The tool's write operations are `add` and `remove`, alongside the read-only `list`,
`graph`, and `profile`. There is deliberately no `update` — correcting a fact is
`remove` then `add`, and enrichment is what `add`'s merge already does, so `update` had no
remaining job. (This is a knowing deviation from the original issue's acceptance
criteria.)

## Injection: transient, and provider-aware

The ledger is never written into `history`. Instead the [conversation loop](conversation-loop.md)
re-serializes it at the top of each iteration into `LoopSession.ledger_block`, and
`_messages_for_provider` inserts it as a `system` message. Because it is transient,
`ContextCompactor` never sees it — no exclusion code is needed.

*Where* it is inserted depends on `provider.caches_prompt_prefix`:

- **Anthropic and DeepSeek** cache a stable prefix, so the ledger goes at the
  **tail** — after the history and any cache breakpoint — so a ledger update never
  invalidates the cached system + tools + history prefix. This trades head-position
  salience for recency salience, to keep the cache warm.
- **Every other provider** gets it **high**, right after the base prompt, where it
  is most salient.

Serialization is memoized on `ledger_revision()` and byte-stable across calls with
no intervening write, so re-injecting each turn is cheap.

## Trimming: bounded, but nothing is deleted

Because the block sits outside compaction, it cannot be reclaimed once it grows — so
`serialize()` trims the *injected copy* to `DEFAULT_MAX_INJECTED_BYTES` (16000,
≈4k tokens). Trimming works on **individual entries**, not whole sections. Entries
are sorted by `_SECTION_PRIORITY`, then least-recently-touched first within a
section:

```
credentials · flags · findings · next_steps · assets · targets · connections · hosts · services
```

That order is then walked greedily, keeping each entry that still fits the budget
(its own rendered line, plus its section header the first time that section is kept).
Two consequences follow from it being greedy and per-entry:

- A section can be **partially** retained. Five credentials against a budget with
  room for three leaves three credentials and a count of the rest, not all or none.
- An entry that does not fit is *skipped*, not a stopping point. One oversized
  high-priority entry can be dropped while smaller lower-priority entries after it
  are still kept.

So the guarantee is directional, not absolute: enumeration (hosts, services) gives
way before conclusions (credentials, flags, findings), but a high-priority fact is
not unconditionally safe. The block appends a count of what it omitted.
**Nothing is deleted** — `ledger list` still reads everything; only the
always-in-context copy is trimmed.

## Softening compaction while the ledger is live

When a ledger is actually open, the [compaction](context-and-compaction.md) prompt is
softened (a `ledger_active` flag): the summarizer is told the creds, IPs, and flags
are separately preserved and it can lean on narrative and attack-chain reasoning
instead of re-listing them exhaustively — while still keeping anything not obviously
a ledger fact.

Critically, that flag tracks the live `FindingsLedger`, **not** `KB_ENABLED`. A
store that failed to open would otherwise license the summarizer to drop the only
copy of a fact.

## Tree-shared, and unmasked

Like the knowledge base, the ledger is shared down the agent tree: delegated agents
both write to it and receive the injected copy. Because tools pass by reference, an
agent's writes are recorded under the master's `engagement_id` — cosmetic, since the
row is tree-shared regardless.

Both front ends (`KerkovCLI`, `WebKerkovSession`) build the `FindingsLedger` beside
the knowledge base, register `LedgerTool` under `KB_ENABLED`, and pass it into their
`LoopSession` — a front end with the mode prompt telling the model to record facts,
but no tool to record them with, would be advertising something it does not have.

Secrets are stored and re-injected **unmasked** — a masked credential is not usable.
Storage is cleartext on local disk with no telemetry, the same posture as the
[knowledge base](knowledge-base.md). Injection is not local, though: the serialized
block is part of every `provider.complete()` request, so against a hosted provider
every turn sends the ledger's credentials and flags in cleartext. That is the
unavoidable cost of the always-in-context guarantee — the control is which provider
Kerkov is pointed at, not the ledger. The `ledger` allow rule is auto-approved in
`config/approvals.yaml` and in its packaged twin `src/kerkov/config/approvals.yaml`;
the two must be kept in step, because `default_approvals_source` falls back to the
packaged copy outside a source checkout.

## Related

- [`ledger`](../reference/tools/ledger.md) — the tool, its operations, and the
  per-section fields.
- [Knowledge base](knowledge-base.md) — the other half of the same `.kb.sqlite`.
- [Context and compaction](context-and-compaction.md) — what the ledger sits
  outside of.
- [Memory guide](../guides/memory.md) — the operator's view.
