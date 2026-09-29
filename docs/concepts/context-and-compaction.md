# Context and compaction

!!! note "Contributor-facing"
    How Kerkov keeps a long engagement inside a finite context window without
    losing the evidence it gathered. The operator's view is in the
    [memory guide](../guides/memory.md).

A long engagement produces more conversation than any context window holds.
Kerkov's answer is **compaction**: when token usage crosses a threshold, the oldest
part of the history is condensed, in a fixed order that protects the things worth
keeping. The machinery is `ContextCompactor`
(`src/kerkov/context/context_compactor.py`), driven from the conversation loop's
`_check_compaction` step.

## The trigger is authoritative, not estimated

Compaction runs when `TokenTracker.total_tokens` crosses
`COMPACTION_THRESHOLD_PCT` (0.65) of `MAX_CONTEXT_TOKENS`. That total is set from
the provider's `response.usage` after each completion — there is deliberately **no
local byte- or character-based token estimator**. An estimate that drifted from the
provider's accounting would either compact too early (wasting the window) or too
late (a hard context error), so the check waits for the authoritative number and
corrects on the next turn.

`_check_compaction` runs straight after a completion, which is why the compactor
does not re-estimate the total itself: the next `response.usage` will correct it
before the next check.

## The fixed order

One compaction does three things, always in this order:

1. **Evict large tool results** — replace bulky tool output with a handle.
2. **Summarize the remaining narrative** — condense the reasoning into one summary.
3. **Roll summaries up** — merge summaries when the block itself outgrows its cap.

Doing eviction first means the summarizer never has to read a 50 KB scan dump to
write one sentence about it; the dump is already a handle by the time it is
summarized.

## Selection: what is safe to compact

`select_messages_to_compact` returns the `[start, end)` range to compact, and it
protects three regions:

- **The system prompt** (index 0).
- **The summary block** — summaries already produced, which form a contiguous block
  at the head (each new one is inserted after those already there). Skipping it
  keeps a summary from being fed back through the summarizer as if it were fresh
  narrative.
- **The recent window**, measured by `preserve_boundary`.

### The recent window is bounded by bytes, not just count

`preserve_boundary` keeps `PRESERVE_RECENT_TURNS` (6) messages — but *subject to*
`PRESERVE_RECENT_BYTES` (65536). Count alone was the original rule, and it had a
failure mode: one oversized message (a `recall(full=true)` expansion, deliberately
not clipped on the way in) would pin the whole six-message window, with eviction
structurally forbidden from touching it, and the engine would sit over threshold
reclaiming nothing. The byte budget breaks that pin.

The budget counts serialized `content` and `raw_blocks`, so tool-call arguments and
reasoning count even when `content` is empty.

### Keeping tool exchanges intact

A `tool_use` must always be answered by its `tool_result` — a provider rejects a
`tool_result` whose `tool_use` was compacted away, and vice versa. So selection
never splits a group:

- The end boundary is walked *back* out of a trailing run of tool results.
- If that walk-back would consume the whole range or exceed the byte budget,
  `_forward_group_boundary` walks *forward* past the run instead — keeping the group
  intact from the other side, once a later message shows the model has read those
  results.
- A tool exchange that reaches the very end of history has no forward boundary
  (anything after it would be an orphaned result), so it must stay intact; if no
  earlier messages are eligible either, selection returns `None`.

Selection **fails loudly.** A `None` return is logged, and `_check_compaction`
warns once (rather than treating a falsy return as a silent success) — the engine
used to ask for compaction, get nothing, and say nothing, every turn.

## Eviction: replace the body, keep the header

`evict_tool_results` rewrites a *copy* of the selected range before summarizing, and
replaces **only the output body**. `split_tool_output` cuts at the output marker —
which is why `format_tool_result` renders output last.

The header it leaves standing carries the tool name, status, error, and metadata.
Those live *only* in the conversation message; the [knowledge base](knowledge-base.md)
artifact behind the result holds `ToolResult.output` alone. Collapsing the whole
message would destroy the metadata with no way to recall it back. The eviction floor
(`EVICT_TOOL_RESULT_MIN_BYTES`, 1024) is measured against the body for the same
reason — a metadata-heavy result with a short output has nothing worth evicting.

The body becomes one of two things:

- **A recall handle** (`build_evicted_tool_reference`) — a bare `artifact#N` — when
  the message has a stamped `artifact_ref`. Fully recoverable.
- **A head/tail clip** (`build_evicted_tool_clip`) when it does not — the knowledge
  base was off, or ingestion failed. Unrecoverable.

Every candidate over the floor is evicted; there is no largest-first ordering,
because the floor, not a byte budget, is the cutoff.

## Carrying artifact handles deterministically

The replacement history is built from the summarizer's text, so an `artifact#N` it
happened to omit would be the last copy of that handle — the messages that held it
are dropped in the same step. Handles are therefore carried **deterministically**,
not by prompt alone:

- `collect_artifact_refs` gathers every id in the range — stamped `artifact_ref`
  plus any written inline in the text.
- `build_artifact_index` appends them to the summary verbatim.

Rollup does the same over the summaries it merges, so handles survive every level.
The prompts still ask the model to keep handles in place next to what they were —
the index is the floor, not the intended form. It is capped at
`MAX_INDEXED_ARTIFACTS` (60, newest kept), because it rides outside compaction like
the summaries.

## Rollup: bounding the summary block

The summary block is excluded from every compactable range, so without a second
mechanism it would grow forever — a long engagement gains a level-1 summary every
compaction, and eventually crosses the threshold with nothing left to compact.
`roll_up_summaries` runs two bounded passes, both asking the model for a *merge*,
not a further compression:

- **Level-1 → level-2:** when the **whole summary block** exceeds
  `MAX_SUMMARIES_BEFORE_ROLLUP` (4) *and* at least two level-1 summaries exist, the
  level-1 run merges into one level-2. Both conditions are on the block, not on the
  run: the level-1 run does not have to reach four itself. A block of `[2, 2, 1, 1, 1]`
  is five summaries with three level-1s, so it collapses to `[2, 2, 2]` in a single
  merge call.
- **Level-2 → level-2:** when the level-2s exceed `MAX_ROLLED_UP_SUMMARIES` (4),
  they merge among themselves.

The second pass is what actually bounds the block. Detail at the head therefore
decays more than once — the merge prompt keeps that as slow as it can, and the
[findings ledger](findings-ledger.md) holds the load-bearing facts losslessly
outside compaction regardless. This is a bounded cap, not a general summary
hierarchy.

## Persistence: compaction rewrites the transcript

Compaction is not just an in-memory trim. `_check_compaction` calls
`override_engagement_messages`, which **rewrites the engagement JSONL with the
compacted history** — the pre-compaction transcript is gone from disk. This is the
point: `Message` carries two persisted provenance fields so a compacted state
survives a `/resume`.

- `summary_level` — `None` for ordinary messages, `1` for a summary, `2` for a
  rolled-up one. Without persisting it, the summary marks would be lost on resume
  and the next compaction would re-summarize summaries as if they were narrative.
- `artifact_ref` — the knowledge-base artifact behind a `tool` message, stamped
  whether or not the result was clipped. A sub-threshold result has an artifact too,
  and that is exactly the case eviction exploits: it can turn a small-in-history
  result into a handle because the full body is already stored.

## The summarizer is billed but not counted as size

The summarization and rollup calls go through the provider like any other, and their
cost is recorded via `record_side_spend` — added to cumulative spend (so the number
is honest) without replacing `total_tokens` (which is the size of the live context
and drives the next compaction). Replacing the total with a summarizer's prompt size
would misreport the conversation.

## Related

- [The conversation loop](conversation-loop.md) — where `_check_compaction` sits.
- [Knowledge base](knowledge-base.md) — the artifacts that eviction turns handles
  into, and `recall` turns back.
- [Findings ledger](findings-ledger.md) — the facts kept outside compaction
  entirely.
- [Memory guide](../guides/memory.md) — what all this means from the operator's
  side.
