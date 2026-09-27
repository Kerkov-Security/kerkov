# Knowledge base

!!! note "Contributor-facing"
    How every tool result is stored, clipped, and made recoverable by `recall`.
    The tool itself is documented in [`recall`](../reference/tools/recall.md); the
    operator's view is the [memory guide](../guides/memory.md).

The knowledge base is the archive behind [`recall`](../reference/tools/recall.md):
the full output of every tool run, indexed so it stays retrievable even after the
conversation has been [compacted](context-and-compaction.md) down to a short
handle. It is a SQLite database with an FTS5 full-text index
(`src/kerkov/core/knowledge_base/engagement_kb.py`), created lazily on the first
write.

## One store per engagement *tree*

The file is `~/.kerkov/engagements/<root-engagement-id>.kb.sqlite`, and the key
detail is **root**. A delegated agent gets its own `engagement_id`, so keying the
file on that id would give each agent a private database its master could never
read. Instead:

- `engagement_id` is a *column*, not part of the filename.
- The file is named for the root of the engagement tree.
- `SpawnAgentTool` passes the master's `EngagementKnowledgeBase` down to each
  `AgentRunner` rather than letting agents open their own.

So `recall` searches the whole tree by default, and a master can drill into the raw
output behind a delegated agent's summarized findings by passing the agent's
engagement id.

`resolve_root_engagement_id` walks `parent_engagement_id` to the root. Today's
spawn wiring hands nested spawn tools the *master's* store, so every agent records
the master as its parent and the walk resolves in one hop — but it is written as a
bounded loop (capped at 16 hops) so a later fix to that parentage keeps working
rather than silently keying a deep agent to the wrong file.

Lifecycle follows the root: `delete_engagement` removes the `.kb.sqlite` only for a
root engagement. Deleting an agent sub-run drops just its rows, because the file
belongs to its master.

## Ingestion happens in the shared engine

Every tool result is ingested by `_ingest_and_clip` in the
[conversation loop](conversation-loop.md), so CLI sessions, web sessions, and agents
all get it. The sequence for one result:

1. **Store the unmodified body.** `_ingest_tool_output` writes the full
   `ToolResult.output` to the store, classified by `ArtifactKind`, sourced by tool
   name, titled by the call's target. Ingestion failure is logged and swallowed —
   indexing is a bonus and must never fail a tool call.
2. **Normalize the copy that enters history.** The conversation keeps a bounded
   version; the store keeps the whole thing.
3. **Replace with a handle when large.** If the stored artifact exists and the
   result's footprint exceeds the clip threshold, the history copy becomes an
   `artifact#N` reference.

Normalization runs *independently of the knowledge base* — a result is trimmed for
history whether or not ingestion succeeded, so the context window is protected even
with `KB_ENABLED=false`. The consequence is stated plainly: with the KB off, output
trimmed out of history is not recoverable, because there is nothing to recall it
from.

## `ArtifactKind`: only what a tool can produce

Artifacts are classified by `artifact_kind_for_tool`:

| Kind | Producing tool |
|---|---|
| `file` | `read` |
| `http_response` | `http` |
| `tool_output` | everything else |

The enum has exactly these three members, deliberately. An unreachable value would
become a `recall(kind=...)` filter that returns confidently empty results forever.
`credential` and `note` are pointedly *absent* — those facts belong in the
[findings ledger](findings-ledger.md) as its own sections, not as an artifact kind.

## Clipping: the footprint is output *plus* metadata

The clip threshold, `KB_CLIP_THRESHOLD_BYTES` (8000), is checked against
`history_footprint_bytes` — the serialized **output and metadata together**. This
matters because `format_tool_result` renders both, and some tools (notably `http`)
put a parsed copy of the response body in metadata. Measuring output alone would let
that second copy sail past the threshold and into history.

So when a result is clipped:

- Oversized metadata values are elided (`build_clipped_metadata`), not just the
  output — otherwise the parsed-body copy would leak in even as the output was
  trimmed.
- The output body is replaced with a clipped reference carrying the `artifact#N`
  handle.

A result whose stored output is changed by normalization gets a handle **even below
the clip threshold**, because that is exactly the small-result case that
[eviction](context-and-compaction.md) later exploits: a stamped `artifact_ref` lets
a modest result be turned into a pure handle when the window fills.

## Two tools are special-cased

- **`recall`** is in `KB_INGEST_SKIP_TOOLS`: its output is not re-ingested (that
  would archive the archive) and, crucially, it is exempt from clipping. Explicitly
  expanding a stored `artifact#N` must return the full body — clipping it would hide
  the very middle the operator asked to see.
- **`read_url`** is in `KB_CLIP_EXEMPT_TOOLS`: its pages are indexed and stamped with
  a handle, but kept inline under their own `READ_MAX_CHARS` budget. A second round
  of clipping would hide part of the requested head before `next_offset`, so
  continuing from the paging marker would skip text nobody ever saw. The pages are
  still evictable later and recoverable through `recall`.

## Search

FTS5 gives ranked full-text search across `title`, `source`, and `body`, kept in
sync with the artifacts table by triggers. Search is entirely local and offline by
default — nothing leaves the machine, and there is no embedding service in the
default path.

The schema reserves an `artifact_embeddings` table and `KB_EMBEDDINGS` opts into
semantic recall, but no concrete embedding backend ships yet, so it is currently
inert. Concurrency is handled with WAL journaling and a `busy_timeout`, so one
agent's `recall` can read while a sibling writes.

`recall`'s `source` filter is matched against both the tool name and the artifact
*title* — where the target lands (`http: <url>`, `read: <path>`) — because the
stored `source` column only ever holds the tool name. See
[`recall`](../reference/tools/recall.md) for the query surface.

## Privacy posture

The store is plaintext on local disk and holds whatever the tools returned,
harvested credentials included. Nothing is redacted and nothing is encrypted.
Kerkov adds no telemetry of its own: the database file, the FTS5 index, and every
`recall` query stay on the machine, and nothing is forwarded anywhere on its own.

Retrieval is a different question. A `recall` result is returned into model history
like any other tool result, and the next `provider.complete()` sends that history —
so against a hosted provider, expanding an artifact that contains harvested
credentials transmits them to that provider. Storage is local; what you pull back
into context is not. The [findings ledger](findings-ledger.md) shares the file and
has the stronger version of the same property: it is injected into *every* request,
not only the ones that ask for it.

## Related

- [`recall`](../reference/tools/recall.md) — the query surface over this store.
- [Findings ledger](findings-ledger.md) — the other half of the same `.kb.sqlite`.
- [Context and compaction](context-and-compaction.md) — how handles are created and
  redeemed.
- [The conversation loop](conversation-loop.md) — where ingestion runs.
