# Issue #152 — Hierarchical, tool-result-aware compaction

Status: implemented on `152/hierarchical-compaction`
Issue: https://github.com/Kerkov-Security/kerkov/issues/152
Base: `main` @ d69c88b

## Scope decision

The issue proposes three changes. This plan ships two of them in full and reduces
the third, based on a read of the code as it stands after #146 (knowledge base)
and C3 (findings ledger) landed:

| Issue section | Decision |
|---|---|
| 1. Never re-summarize a summary | **In scope.** Confirmed defect, cheap fix. |
| 2. Tool-result-aware eviction | **In scope, narrowed.** #146 already evicts the large blobs at ingest; what remains is compaction-time re-clipping and the no-KB path. |
| 3. Hierarchical rollup | **Descoped to a bounded cap.** Full hierarchy deferred until long engagements demonstrate the need. |

The issue also implies a dependency on #150 (C2, tool-output normalization). There
isn't one: `build_clipped_output` already exists in `conversation_engine.py` and
only needs an artifact-less variant. The two overlap but neither blocks the other.

## Findings from the code review

### The re-summarization defect is real

`ContextCompactor.compact()` inserts its summary at index `start` (always 1) as a
plain `role="assistant"` message. `select_messages_to_compact()` returns
`(1, len(history) - PRESERVE_RECENT_TURNS)` unconditionally. So at the next
compaction the summary sits at index 1, inside the range, and is fed back through
`build_summarization_prompt()` as if it were fresh narrative. Every compaction
re-summarizes every prior summary.

`ConversationEngine._check_compaction` then calls
`EngagementStore.override_engagement_messages(updated_history)`, which **rewrites
the engagement JSONL with the compacted history**. The pre-compaction transcript
is gone from disk. Decay here is permanent — there is no re-reading the original.

### The mark has to survive `/resume`

This is the part the issue does not mention and the part most likely to be missed.
`Message` and `EngagementEntry` (`cli/engagement_store.py`) are separate models. A
new field on `Message` alone is dropped on persist, so after `/resume` the
summaries are unmarked and the next compaction re-summarizes them again — with
every in-memory test still passing. The field must be threaded through
`EngagementEntry`, both write paths (`append_message`,
`override_engagement_messages`), and the message reconstruction in `load_engagement`.

### Blob eviction is mostly already done

`ConversationEngine._ingest_and_clip` replaces any tool result whose
`history_footprint_bytes` exceeds `KB_CLIP_THRESHOLD_BYTES` (8000) with an
`artifact#N` reference plus a 1500/500-char preview, **at ingest time**, before it
reaches history. When `KB_ENABLED`, compaction never sees a giant blob. What is
actually left:

- **Sub-threshold accumulation.** Forty 6 KB results are ~240 KB of history that
  never trips the per-result threshold, and compaction currently spends an LLM
  summarization pass on all of it.
- **Compaction-time re-clipping.** A tool message inside the compactable range can
  drop to a bare one-line `artifact#N` with no preview instead of going to the
  summarizer. Lossless — the full text is in the KB.
- **KB disabled or ingestion failed.** Nothing is clipped at all.

### The compactor cannot currently name an artifact

`ContextCompactor` is constructed with only a tracker and limits
(`kerkov_cli.py`, `web/runtime/session.py`) and has no route from a history tool
message back to its artifact. `EngagementKnowledgeBase` has no lookup by
`call_id` — the id is stored inside the artifact's metadata JSON, neither indexed
nor queryable. Adding such a lookup is more code than the alternative: **stamp the
artifact id onto the tool `Message` at ingest**, reusing the same persisted-field
plumbing step 1 already requires.

### Stale docstring

`compact()`'s docstring claims it "Modifies history in-place and re-estimates the
tracker total from the new history." It does neither — it returns a new list
(`context_compactor_test.py::CompactTest.test_does_not_mutate_input_history`
asserts non-mutation) and never touches the tracker. The behaviour is harmless
because `_check_compaction` only runs immediately after a completion, so the next
`response.usage` corrects the total before the next check. Fix the text, not the
code — and do not "fix" it by adding a local estimator (see Non-goals).

## Implementation

### Step 1 — Persisted message provenance

Two new optional fields on `Message` (`models/basic_models.py`):

- `summary_level: Optional[int]` — `None` for ordinary messages, `1` for a
  compaction summary, `2+` for a rolled-up summary.
- `artifact_ref: Optional[int]` — the artifact id backing a `role="tool"` message,
  when one was ingested.

Thread both through `cli/engagement_store.py`: add them to `EngagementEntry`, set
them in `append_message` and `override_engagement_messages`, and read them back in
`load_engagement`. Both are optional with `None` defaults, so existing
JSONL files load unchanged — no migration.

`ConversationEngine._ingest_and_clip` sets `artifact_ref` on the tool `Message` it
appends, whether or not the result was clipped (a sub-threshold result has an
artifact too — that is the case step 3 exists to exploit).

### Step 2 — Exclude summaries from the compactable range

Summaries always form a contiguous block at the head of the range: each new one is
inserted at `start`, after any already there. So `select_messages_to_compact()`
advances `start` past leading `summary_level is not None` messages before
computing the range, and returns `None` if the remaining range collapses.
`compact()` stamps `summary_level=1` on the message it produces.

The existing tool_use/tool_result walk-back on `end` is untouched.

### Step 3 — Evict tool blobs before summarizing

A pass over the selected range, before `build_summarization_prompt()`:

- For a `role="tool"` message with an `artifact_ref`, replace its content with a
  bare one-line reference (`build_evicted_tool_reference(artifact_id)` — a new
  sibling of `build_clipped_output` in `conversation_engine.py`, no preview).
- Without an `artifact_ref` (KB disabled, or ingestion failed), fall back to a
  head/tail clip with no artifact id — an artifact-less variant of
  `build_clipped_output`.

Ordering is then explicit: **evict tool blobs → summarize the remaining
narrative**. The evicted messages are excluded from the summarization prompt
entirely, so the summarizer only ever sees reasoning.

Selection: largest-first by byte size, per the issue's open question — it reclaims
the most for the least loss. Only evict above a size floor; a 200-byte tool result
costs more in reference boilerplate than it saves.

The eviction helpers live in `conversation_engine.py` next to the existing clip
helpers and are imported by the compactor, keeping one definition of the reference
format the model is taught to read.

### Step 4 — Bounded rollup (not a hierarchy)

When the count of `summary_level >= 1` messages exceeds a cap (start at 4),
combine them into one `summary_level=2` message via a distinct
`build_rollup_prompt()`. Cap the level at 2 — a summary that has been rolled up
once is never rolled up again; it stays verbatim.

This is deliberately not the general hierarchy the issue describes. With step 2 in
place each summary is bounded by `SUMMARY_RESERVE_TOKENS` (2000) against
`MAX_CONTEXT_TOKENS` (50000), and the findings ledger already holds the facts
losslessly outside compaction, so the summary stack needs ~8+ compactions in one
engagement to become a problem. Build the depth-capped hierarchy only if that
turns out to happen.

### Step 5 — Docs

- Fix the `compact()` docstring.
- CLAUDE.md: a short note under the compaction/KB material giving the ordering
  (evict → summarize → roll up), the fact that summaries are excluded from
  re-summarization, and that the exclusion is persisted so it survives `/resume`.

## Tests

`tests/context/context_compactor_test.py` (unittest, `_run` helper, `AsyncMock`
summarize callback, offline):

- **No re-summarization** — after two compactions, the first summary's text is not
  present in the second `build_summarization_prompt()` input.
- **Range selection with summaries present** — `select_messages_to_compact()`
  starts after the leading summary block; returns `None` when that leaves nothing.
- **Eviction before summarization** — a range holding a large tool message and
  some reasoning produces a prompt containing the reasoning and not the blob.
- **KB vs. no-KB eviction** — with `artifact_ref` set the message becomes
  `artifact#N`; without, the head/tail clipped form.
- **Size floor** — a small tool result is left alone.
- **Rollup** — N summaries past the cap combine into one `summary_level=2`; a
  level-2 summary is not rolled up again.
- **Boundary safety** — the existing tool_use/tool_result walk-back tests still
  pass unchanged.
- **Usage reactivity** — `should_compact()` still keys off
  `TokenTracker.total_tokens`; assert no byte/char estimator was introduced.

`tests/cli/engagement_store_test.py`:

- **Round-trip** — a history containing a marked summary and a tool message with
  an `artifact_ref` survives `append_message` → read and
  `override_engagement_messages` → read with both fields intact. **This is the
  test the issue omits and the one most likely to catch a broken implementation**
  (see "The mark has to survive `/resume`").
- **Backward compatibility** — an existing JSONL entry without the new keys loads
  with both fields `None`.

## Non-goals

- **No local token/char estimator.** Compaction stays reactive to authoritative
  `response.usage` via `TokenTracker`, per the tracker docstring and recorded
  project convention. This includes not "fixing" the stale `compact()` docstring
  by making it true.
- **General depth-capped summary hierarchy** — deferred, see step 4.
- **Tool-output normalization heuristics** — #150 / C2 owns those.
- **Provider-native / server-side compaction** — out of scope.

## Files touched

- `src/kerkov/models/basic_models.py` — two optional `Message` fields.
- `src/kerkov/cli/engagement_store.py` — persist and read them.
- `src/kerkov/context/context_compactor.py` — summary exclusion, eviction pass,
  rollup, docstring.
- `src/kerkov/core/conversation_engine.py` — stamp `artifact_ref`; eviction helpers.
- `tests/context/context_compactor_test.py`, `tests/cli/engagement_store_test.py`.
- `.claude/CLAUDE.md` — compaction ordering note.

Estimated ~300-400 lines including tests. One PR.

## Deviations from this plan, as built

Three, all found while writing the code:

- **The eviction helpers could not live in `conversation_engine.py`.** It already
  imports `ContextCompactor`, so importing back the other way is a cycle. The
  reference builders (`format_bytes`, `build_clipped_*`, `build_evicted_*`) moved
  to a new leaf module `src/kerkov/core/tool_output.py`, which both import.
  `conversation_engine` re-imports them, so its public surface — and every
  existing test that imports from it — is unchanged.

- **Evicted messages go to the summarizer as references, not excluded from the
  prompt.** Step 3 said both. Dropping them entirely would throw away the
  `artifact#N` handle, which is the only thing the model can still act on once
  the range is replaced; keeping them out of history is not an option either,
  because a tool message whose `tool_use` was just compacted away is invalid for
  Anthropic. So the eviction pass rewrites a *copy* of the range, and both
  summarization prompts now instruct the model to carry every `artifact#N`
  forward verbatim.

- **No largest-first ordering.** With a size floor and no byte budget, every
  candidate above the floor is evicted, so ordering changes nothing; adding it
  would be dead code. A real budget was considered and dropped — it edges toward
  the local-estimation the issue rules out.

Everything else shipped as written, including the persisted-provenance step and
its round-trip test.
