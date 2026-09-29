# Findings ledger

!!! note "Contributor-facing"
    How the ledger stores confirmed facts, merges them by identity, and stays in
    front of the model across compaction. The tool is documented in
    [`ledger`](../reference/tools/ledger.md); the operator's view is the
    [memory guide](../guides/memory.md).

The findings ledger is the model's structured working memory: a small store of
confirmed facts (`assets`, `targets`, `hosts`, `services`, `credentials`,
`findings`, `flags`, `next_steps`) that is **re-injected at the top of context
every turn**. It is the fix for "the model forgot the credential it found forty
messages ago" — because it lives *outside* the summarized history, [compaction](context-and-compaction.md)
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

## No `update` in v1

The tool exposes `add`, `list`, and `remove` only. There is deliberately no
`update` — correcting a fact is `remove` then `add`, and enrichment is what `add`'s
merge already does, so `update` had no remaining job. (This is a knowing deviation
from the original issue's acceptance criteria.)

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
credentials · flags · findings · next_steps · assets · targets · hosts · services
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
