# The conversation loop

!!! note "Contributor-facing"
    How `ConversationEngine` drives one turn, and the two abstractions that let
    the same loop run the interactive master and an isolated agent.

At the centre of Kerkov is one loop, in `ConversationEngine.run_loop`
(`src/kerkov/core/conversation_engine.py`). It asks the provider for a completion,
runs any tools the model asked for, and repeats until the model answers with prose
or a limit is reached. The CLI master and every delegated agent run *this same
loop* — the differences between them are pushed into a small parameter object and
two abstract strategies.

## `LoopSession`: everything one turn needs

`run_loop` takes a `LoopSession` — a dataclass holding the provider, the mutable
`history` list, the tools, the two strategies below, the caps, and the optional
machinery (engagement store, knowledge base, compactor, token tracker, interrupt
handle, inbox, findings ledger). `history` is mutated in place, so the caller's
reference stays valid across appends and compaction.

The master and an agent differ only in what they put in the session. A few fields
capture the whole difference:

| Field | Master | Agent |
|---|---|---|
| `output` | `ConsoleSink` (renders to the terminal) | `QuietSink` (logs + buffers) |
| `approval` | `InteractiveApproval` (prompts) | `SupervisedAgentApproval` (auto-approves in whitelist) |
| `interrupt_ends_turn` | `False` (Escape stops a tool, keeps the turn) | `True` (a stop ends the run) |
| `inbox` | `None` | a queue of operator messages, drained each iteration |
| `terminate_after_tool` | `None` | stops the loop the moment `report` is called |

## The two strategies

Because the loop is shared, the two things that genuinely differ between contexts
are abstracted:

- **`OutputSink`** — where narration goes. `ConsoleSink` renders to a Rich console
  (the current master behaviour, including streaming assistant text). `QuietSink`
  logs and buffers instead, so an agent running in the background does not fight
  the master for the terminal; the buffer is what an [attach](agent-subsystem.md)
  later replays.
- **`ApprovalStrategy`** — how a tool call is approved. `InteractiveApproval` asks
  the operator. `WhitelistAutoApproval` approves anything inside the profile's
  whitelist, and `SupervisedAgentApproval` wraps it so an attached operator can opt
  into reviewing a specific agent's calls (see the [agent subsystem](agent-subsystem.md)).

Neither the engine nor a tool knows which concrete strategy it has. That is what
lets one loop serve both a human at a prompt and a detached agent with nobody
watching.

## One iteration, step by step

Each pass of the loop:

1. **Stop check.** A cancel that landed between `await`s cannot raise anywhere
   else, so the top of the loop is where it is noticed and the turn breaks.
2. **Drain the inbox.** Operator messages queued for an agent are appended here —
   the only history-valid insertion point, because every earlier `tool_use` already
   has its `tool_result`. Inserting a message between a call and its result would
   make the provider reject the whole turn.
3. **Refresh the ledger block.** The [findings ledger](findings-ledger.md) is
   re-serialized (cheap — memoized on its revision) so a fact recorded earlier this
   turn shows up in the next iteration's injected copy.
4. **Complete.** `_complete` makes one provider call, threading a delta callback in
   when the sink streams so assistant text renders as it arrives — and accumulating
   the chunks so an interrupt mid-generation can preserve what was already shown as
   partial text rather than losing it.
5. **Branch on the response:**
    - **Tool calls** → `_handle_tool_calls` (below), then record usage, then a
      [compaction](context-and-compaction.md) check, then loop again.
    - **Prose** → append it, update the engagement summary, render it, check
      compaction, and end the turn.
6. **Iteration cap.** At `max_iterations` the loop stops with a warning rather than
   looping forever.

## Executing a batch of tool calls

`_handle_tool_calls` runs every call in one model turn, in order, appending a
`tool` message per call so history stays well-formed. For each call:

- If a stop or a prior denial/interrupt means this call should be skipped, a skip
  result is appended in its place — the `call_id` is still answered, because a
  `tool_use` with no matching `tool_result` is rejected by the provider.
- The tool is looked up, the approval strategy is consulted, and on approval the
  tool runs **interruptibly** (`_await_interruptible`), so Escape can cancel an
  in-flight tool.
- The result is [ingested and clipped](knowledge-base.md) — stored in the knowledge
  base, and replaced in history with a compact reference if it is large — then
  appended with its artifact reference.
- If `terminate_after_tool` fires (an agent's `report`), the loop stops after this
  turn.

A stop landing *between* the calls of one batch is caught before the next call
starts, because the async runtime schedules a coroutine regardless of a pending
cancel, and a tool that finishes without yielding would beat the interrupt watcher
and land its side effect anyway. `report` is exempt from stopping — it writes
nothing outside the run and carries the findings a graceful stop exists to preserve.

## Building the provider request

`_messages_for_provider` assembles what actually goes to the provider: the history,
plus the transient prompts (a non-interactive nudge, a verbosity instruction), plus
the ledger block. Neither the ledger nor the transient prompts are written into
`history` — they are rebuilt each turn.

Where the ledger block lands depends on `provider.caches_prompt_prefix`:

- **Providers that cache a stable prefix** (Anthropic, DeepSeek) get the ledger at
  the **tail**, after the history, so a ledger update never invalidates the cached
  system + tools + history prefix. Only the small ledger tail is re-sent uncached.
- **Every other provider** gets it **high**, just under the base system prompt,
  where it is most salient.

This is the one place the loop adapts to a provider capability, and it is a
deliberate trade of head-position salience for cache stability.

## Usage, engagement summary, interrupts

- **Token usage** is recorded from the provider's authoritative `response.usage`,
  which replaces the tracker's total. There is no local token estimator; the
  numbers that drive [compaction](context-and-compaction.md) are the provider's.
- The **engagement summary** is set to the first user message, so a saved
  conversation is recognisable in `/engagements`.
- **Interrupts** are handled differently by role via `interrupt_ends_turn`: the
  master absorbs an interrupt and continues the turn (Escape means "stop this
  tool"), while an agent treats it as terminal and reports what it had.

## Reuse

Everything above runs identically for CLI sessions, web sessions, and agents. Output
normalization, knowledge-base ingestion, and ledger injection all live in this one
engine, so a behaviour added here is a behaviour every caller gets — which is why
new work generally belongs in the engine rather than in a front end.

## Related

- [Context and compaction](context-and-compaction.md) — the `_check_compaction`
  step in detail.
- [Knowledge base](knowledge-base.md) — the ingest-and-clip step.
- [Findings ledger](findings-ledger.md) — the block injected each iteration.
- [Agent subsystem](agent-subsystem.md) — how the same loop runs detached.
