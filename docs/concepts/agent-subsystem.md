# Agent subsystem

!!! note "Contributor-facing"
    This page is the mechanism and the reasoning behind it — module boundaries,
    the delegation flow, cancellation, the terminal plumbing. To *use* agents
    rather than change them, start with the [agents guide](../guides/agents.md);
    the profile schema is in [agent profiles](../reference/agent-profiles.md) and
    the tools in [`delegate` and `agent_wait`](../reference/tools/delegate.md).

Self-contained sub-task delegation: the master conversation hands a clear
instruction to an isolated agent, which runs with a restricted tool set
and reports back a structured result. The noisy intermediate work never
enters the master's context.

Agents are **detached**. `delegate` launches them and returns immediately with
their ids, so the master keeps its prompt while they work; the reports arrive
later, either because the master asked (`agent_wait`) or because the supervisor
delivered them at a turn boundary.

Detachment is the **master's** alone. The nested `delegate` an agent receives
blocks until its children report, because the two collection paths both belong
to the master: `agent_wait` is registered by the front end, and turn-boundary
delivery needs a turn. A detached child would therefore bypass its parent
entirely — the parent would report without its findings while they surfaced in
the master's conversation, attributed to an agent the master never spawned.

## Where things live

| Module | Responsibility |
|---|---|
| `src/kerkov/core/conversation_engine.py` | Shared tool-execution loop (`ConversationEngine.run_loop`). Drives both the master CLI and any agent. |
| `src/kerkov/agents/models.py` | `AgentProfile` pydantic model + `effective_tools()` whitelist intersection. |
| `src/kerkov/agents/base_agent.py` | Built-in profiles: `recon`, `analysis`, `exploit`, `general`, `container-operator`. |
| `src/kerkov/agents/agent_manager.py` | Loads built-ins + `AGENT.md` files; exposes the index injected into the master's system prompt. |
| `src/kerkov/agents/report_tool.py` | `report` tool — every agent has it; calling it terminates the agent loop. |
| `src/kerkov/agents/agent_runner.py` | Runs one agent's isolated loop and returns an `AgentRunReport`. |
| `src/kerkov/agents/agent_registry.py` | Process-wide depth + total-agent caps (budget *policy*). |
| `src/kerkov/agents/agent_session.py` | `AgentSession` — one live agent as a nameable object (`a1`), its state, status, live history, and event sink. |
| `src/kerkov/agents/agent_supervisor.py` | Session-scoped owner of every running agent: tasks, ids, the shared workspace, cancellation, report delivery (instance *registry*). |
| `src/kerkov/agents/agent_tool.py` | `delegate` tool — the master calls this to launch N detached agents at once. |
| `src/kerkov/routing/` | Provider-independent task context, deterministic routing policy, and model-selection decision. |
| `src/kerkov/agents/agent_wait_tool.py` | `agent_wait` tool — blocks until named (or all outstanding) agents report. |
| `src/kerkov/agents/report_format.py` | The single report formatter shared by `agent_wait` and turn-boundary delivery. |
| `src/kerkov/agents/agent_approval.py` | `SupervisedAgentApproval` — auto-approve within the whitelist unless the watching operator asked to review. |
| `src/kerkov/cli/agent_focus_manager.py` | `AgentFocusManager` — whether the terminal is pointed at the master or at one agent. |
| `src/kerkov/cli/agent_attach.py` | `AgentAttachView` — replays a focused agent's output, then follows it live. |
| `src/kerkov/cli/buffering_sink.py` | `BufferingSink` — records the master's narration while attached, replays it on detach. |
| `src/kerkov/cli/agent_display.py` | One rendering of a session, shared by `/agents`, the picker, the attached prompt, and the strip under the prompt. |
| `src/kerkov/cli/agent_review.py` | `AttachedAgentReview` — the terminal half of tool review: who may ask, and how the question reaches the screen. |
| `src/kerkov/cli/agent_transcript.py` | Read-only rendering of a finished agent's transcript, for `/resume <agent-id>`. |
| `src/kerkov/cli/messages/agents_message.py` | `/agents`, `/agents stop`, `/agents review` and `/back` handlers. |

## How a delegation flows

1. Master LLM emits a `delegate` tool call with a list of `AgentTaskSpec`s.
2. The master's approval strategy checks the `delegate` batch against the current
   mode and rules. With the packaged CLI rules, `strict` and `normal` prompt;
   `permissive` and `yolo` auto-approve. The prompt can save an exact-call rule
   for reuse in `normal` mode (see
   [delegation approval](../reference/tools/delegate.md#approval)).
3. On approval, `SpawnAgentTool.run()` turns each task into an
   `AgentSpawnSpec` and hands the batch to `AgentSupervisor.spawn()`. The spec
   carries a `build_runner` closure, because resolving a profile into a tool
   set (and reconstructing the nested `delegate` at the child's depth) is the
   tool's job while lifecycle is the supervisor's.
4. `AgentSupervisor.spawn()`:
   - Checks depth + total caps via `AgentRegistry`, then the live
     `AGENT_MAX_CONCURRENT` gate. All raise `BudgetExceeded`; the concurrency
     one raises the `ConcurrencyExceeded` subclass, because it is the only cap
     that clears on its own and the advice differs by caller — the master is
     told to collect with `agent_wait`, while a sub-agent (which has no
     `agent_wait`, and whose siblings are the ones holding the slots) is told
     to do the work itself.
   - Allocates an id (`a1`, `a2`, … monotonic for the whole session, never
     reused) and a display name (see below), resolves the shared run workspace
     once, builds each runner, and wraps `runner.run()` in an `asyncio.Task`.
   - Returns the `AgentSession` handles **immediately**.
5. `delegate` returns a result that names the agents, their profiles, and their
   tasks, and states the collection step. It contains no findings. A nested
   `delegate` (`detached=False`) instead awaits `supervisor.wait(ids=…)` on
   just the ids it spawned — scoped, so one agent cannot consume its siblings'
   or the master's undelivered reports — and returns them. If the parent is
   cancelled mid-wait it requests cancellation of those children on its way
   out, so none are left running with nobody to collect them.
6. Each `AgentRunner`, now running detached:
   - Builds a fresh history (profile system prompt + instruction) into its
     `AgentSession`, so an attached view reads the live list.
   - Creates its own `EngagementStore` (linked to the parent by
     `parent_engagement_id`).
   - Builds its own `TokenTracker` and `ContextCompactor`, targeting the
     agent's own context window (`provider.get_max_context_tokens()`, else
     `MAX_CONTEXT_TOKENS`). An agent that cannot reclaim context has only the
     iteration cap between it and a provider context-limit error, which the
     master reads as `stuck` rather than as the partial findings the run had
     already earned. The tracker is shared with the `AgentSession`, so
     `/agents` and `supervisor.total_agent_tokens()` read it live. Compaction is
     billed too: the summarizer/rollup call goes through
     `TokenTracker.record_side_spend`, which adds to `cumulative_tokens`
     (spend, hence the report) without replacing `total_tokens` (size, which
     drives the next compaction). A terminating `report` breaks the loop *before*
     the post-turn compaction check, so a summarizer failure can never bury a
     report the agent already produced.
   - Runs against a **shallow copy of the provider**, so the model it started
     on is the model it finishes on: `/model` and `/provider` mutate the one
     shared instance under in-flight agents.
   - Runs the shared `ConversationEngine` with the session's `AgentEventSink`
     (a `QuietSink` that also keeps `iteration` / `current_tool` / state
     current) and a `SupervisedAgentApproval` — whitelist auto-approval unless
     the operator is attached and asked to review this agent's calls.
   - The engine's `terminate_after_tool` hook stops the loop immediately when
     the agent calls `report`.
7. The report reaches the master one of two ways, and exactly once either way
   (`AgentSession.report_delivered` is the bookkeeping):
   - **`agent_wait(ids=…, timeout_s=…)`** — the model asks. A timeout returns
     the reports that are ready plus a `Still running: a3` line rather than
     raising, which would strand finished agents' work.
   - **Turn-boundary delivery** — at the top of each master turn the front end
     calls `supervisor.drain_finished_reports()` and appends each undelivered
     report as a `user` message. This is the anti-fire-and-forget mechanism:
     a mechanism, not a prompt instruction.
   A turn boundary needs a turn, so the CLI also *makes* one: a report landing
   while the prompt sits idle ends that prompt (`InputManager.wake_chat_prompt`
   → `WAKE_SENTINEL` → `AgentReportWakeMessage`), and the master takes a turn
   carrying the report and no user message. Without it a fire-and-forget
   `delegate` whose findings arrive after the master ran out of work would wait
   for a keystroke. Only a **master-owned** report (a `top_level` session, spawned
   at `parent_depth==0`) wakes the prompt — a nested child reports to its parent,
   which is blocked on `agent_wait` for it, so waking the master would produce a
   turn with nothing of its own to deliver. `AgentReportWakeMessage` re-checks
   `supervisor.has_undelivered_reports()` before running the engine, so a report
   collected between the wake and the turn (by that `agent_wait`, say) does not
   run an empty turn on unchanged history. The wake itself is skipped while the
   operator is attached to an agent (the prompt is that agent's), while a picker
   or approval box owns stdin (`wake_chat_prompt` only fires on the `wakeable`
   main chat prompt, never the resume picker that reuses the same session), and
   mid-turn — a wake dropped mid-turn cannot be lost, because the run loop
   re-checks `has_undelivered_reports()` before sitting back down at the next
   idle prompt. Typed text is stashed and restored like a takeover's, and a
   second report finishing inside one prompt teardown is idempotent so the first
   wake's stashed draft survives.
   Both paths render through `agents/report_format.py`, so the model only ever
   learns one shape. Each agent's `engagement_id` rides along so the user (or
   `recall`) can open the full transcript, and so does `tokens_used` — agent
   spend never reaches the master's `TokenTracker` (that scalar is the size of
   the master's own context and drives its compaction), so the report and the
   `/agents` table are the only places it surfaces.
8. On quit — `/q`, `Ctrl-C`, or EOF — the CLI awaits `supervisor.cancel_all()`
   so no agent is orphaned, and prints the resume ids. A one-shot
   (`--message`) run instead waits for outstanding agents, records their
   reports in the transcript, and prints them, since there is no next turn to
   deliver into.
9. On a conversation switch — `/clear` or `/resume` — the CLI calls
   `settle_agents_for_conversation_switch()`: cancel, then
   `discard_pending_reports()`. A report is addressed to the engagement that
   spawned it, but turn-boundary delivery fires against whatever conversation
   is live at the next turn, which would file (and persist) one engagement's
   findings under another.

## Choosing a model for delegated tasks

Model routing is opt-in and applies to future delegated tasks. Configure
`AGENT_ROUTING_SMALL_MODEL`, `AGENT_ROUTING_MEDIUM_MODEL`, and
`AGENT_ROUTING_LARGE_MODEL` with three distinct model IDs supported by the
current provider, then use `/model auto`. The main conversation keeps its
selected model. Set `AGENT_ROUTING_ENABLED=true` to enable routing at startup.
Invalid or duplicate tier model settings log a warning and fall back to manual
delegation.
Selecting a concrete model with `/model <id>` or the model picker disables
auto mode after the selection succeeds; switching providers disables it as
well. Running agents keep their original model.

The router makes one deterministic decision per task, without an extra model
call. It has three tiers:

| Tier | Explicit task kind | Eligible work | Instruction limit |
| --- | --- | --- | --- |
| SMALL | `extract` or `format` | Literal extraction or reformatting of supplied content, without reasoning or decisions. | 2,000 characters |
| MEDIUM | `analyze` | Routine, benign analysis of supplied facts using read-only tools. | 4,000 characters |
| LARGE | Any other case, including the default `general` | General, complex, security-sensitive, actionable, or uncertain work. | No routing limit |

SMALL and MEDIUM both require the original built-in `analysis` profile, an
effective tool set containing only `read` and/or `recall`, nonempty instructions,
and nonempty success criteria of at most 1,000 characters. Custom profiles,
including one shadowing `analysis`, use LARGE. Language indicating security
decisions, actions, or clearly complex work also selects LARGE. SMALL applies
additional screening for reasoning or analysis. An ineligible task goes to
LARGE; the router does not promote an oversized extraction task to MEDIUM.

For example, a built-in `analysis` task with `task_kind="extract"`,
`allowed_tools=["read"]`, instruction "Extract all HTTP status codes from
the supplied log", and success criteria "Return a JSON array of the codes"
is eligible for SMALL. With the same profile and tools, `task_kind="analyze"`,
instruction "Compare the supplied monthly totals", and success criteria
"Describe the largest changes using the supplied numbers" is eligible for
MEDIUM. A general task or a security assessment uses LARGE. These conservative
heuristics choose a cost tier; they cannot guarantee task difficulty and do not
replace scope or tool approval checks.

Each agent receives a separate provider instance fixed to its selected model.
SMALL and MEDIUM agents do not receive `delegate`, so they cannot obtain broader
tools by spawning a child. LARGE and manual agents retain nested delegation
when the depth cap permits it; their children are routed using the provider and
policy snapshots captured when the parent was spawned. Changes to the main
conversation's model or auto-mode setting affect subsequent top-level delegations.

The delegate result, `/agents` picker, attached output, and saved transcript
show the selected provider, model, tier, and reason. Agent JSONL metadata keeps
the same selection for later inspection; older transcripts remain readable.
`/usage [engagement-id]` summarizes the selected engagement and its descendants,
including the main conversation when it is selected. Each provider call records
reported tokens, cache usage, model, tier, duration, and completion/failure/
cancellation. Agent outcomes and main-conversation turn outcomes are recorded
separately; completion does not verify answer quality. Measurements live in
append-only `usage/<engagement-id>.jsonl` files beneath the engagement store, so
transcript compaction does not erase them. Old transcripts remain readable and
their unmeasured history is explicitly unknown.

Optional `MODEL_PRICING_FILE` rates produce cache-aware estimates in USD. Missing
usage or prices keep total cost unknown while retaining known subtotals. See
the [routing evaluation guide](routing-evaluation.md) for the fixed fixture
comparison of automatic routing against LARGE. Model-based grading, escalation,
cross-provider routing, and library extraction remain future work.

## Naming: `a1` inside, `google-pinger` outside

An agent has two handles and they do different jobs:

- **`id`** (`a1`, `a2`) — the internal address. Monotonic across the session,
  never reused, never derived from anything the model wrote, so it cannot
  collide or be ambiguous. It is what `agent_wait`, `/agents`, and logs key on.
- **`name`** (`google-pinger`) — what the **user** sees. `delegate` takes an
  optional `name` per task; the master writes it in the same tool call as the
  instruction, so a good name costs no extra LLM round-trip. It is slugified to
  kebab-case and made unique within the session (`port-scan-2`).

When the master does not supply one, `slugify_agent_name` derives it from the
instruction, then from the profile — a name always exists, because it is the
only handle the CLI prints. The slug splits on whitespace before punctuation so
`10.0.0.5` survives as one word; a bare count (`50`) is dropped as noise.

`AgentSupervisor.get()` and `wait(ids=…)` resolve **either** handle, since the
model is shown both. User-facing CLI output prints `display_name()` only;
model-facing text carries `name (id, profile)` so the model always has an
unambiguous thing to pass back.

## Watching an agent: focus, `/agents`, and attach

Agents run detached, so the terminal has to answer "which conversation am I
looking at". `AgentFocusManager` is that single answer: the **master**, or one
agent id. Focus is held by **id**, never by name — a name is a display label
the model chose, and resolving it late would let a rename move focus.

`/agents` lists every agent spawned this session (active and finished) and
opens the provider-style picker to switch. `/agents <name|id>` attaches
straight away, `/agents master` and `/back` return, and `Ctrl-O` opens the
picker without typing anything — the text already typed is stashed and
restored as the next prompt's default, so switching never eats a half-written
message. Esc cancels the picker and changes nothing.

`Ctrl-O` works **mid-turn** as well as at the prompt, which is the point: an
agent is only interesting while the master is still working. The two paths are
different mechanisms. At the prompt it is a prompt_toolkit binding that exits
with `SWITCHER_SENTINEL`. During a turn the cbreak watcher recognises the byte
and sets `InputManager.switcher_requested`, which a task started for the
duration of `run_loop` (`_watch_for_switcher`) is waiting on.

The signal has to live on the manager rather than in the watcher thread,
because `ConversationEngine._await_interruptible` starts and stops that thread
around **every** awaited coroutine — each LLM completion and each tool call —
so the thread that saw the key is gone before the picker opens. Three
consequences:

- A key pressed in the gap *between* awaits is still dropped, though less
  crudely than it once was. The turn now holds the terminal in cbreak for its
  whole duration (`InputManager.hold_raw_mode`/`release_raw_mode`), so a key
  struck between awaits no longer echoes or blocks until Enter the way it did
  when each watcher flipped the terminal mode back on teardown. The *reader*,
  though, is not held across the turn: each watcher calls `_drain_stdin` at
  startup to discard input buffered before it began, so a byte pressed while no
  watcher owned stdin is thrown away. Holding the reader too, not just the mode,
  is still a re-architecture of `_await_interruptible`. This is pre-existing for
  Escape.
- A tool call finishing while the picker is open would otherwise start a second
  cbreak reader on the same fd — a wedged terminal, not an exception.
  `begin_switcher()`/`end_switcher()` set `_switcher_active`, and
  `start_watching_for_interrupt` is inert while it is set. A lock would work
  too but would stall the turn behind a UI choice. The cost is that Escape does
  not interrupt while the picker is open, which is right: Escape is the
  picker's own cancel key.
- The picker is shielded from the watcher task's cancellation, so a turn that
  finishes while a menu is open does not yank it out from under the user.

Mid-turn `Ctrl-O` needs a POSIX TTY (`_POSIX_TTY_AVAILABLE`); on Windows the
watcher does not run and only the prompt-level binding is available.

Attaching **replays then streams**. `AgentAttachView` prints the tail of the
agent's `QuietSink` buffer (the last `REPLAY_TAIL_LINES`, since a long run's
buffer is thousands of lines) and then subscribes to `AgentSupervisor.subscribe`
for everything after it, filtered to the focused session. A cursor into the
buffer is what keeps replay and stream from overlapping. Attaching to an agent
that already finished shows the same recorded output, marked as finished.

### Talking to the agent you are attached to

Typed chat while attached goes to that agent, never to the master — the user is
looking at the agent's output and would be addressing that. Slash commands are
routed by message type and so are untouched: `/back` and `/q` always work.

The path is `KerkovCLI._route_input_to_focused_agent` → `session.inbox` (an
`asyncio.Queue` on the `AgentSession`) → `ConversationEngine._drain_inbox`,
which empties the queue at the **top of each `run_loop` iteration** and appends
each line as a `user` message through `_append`, so it lands in the agent's
history *and* its own JSONL in one place.

The iteration top is the only correct drain site. Anywhere else — inside
`_handle_tool_calls`, say — a user message can be inserted between a `tool_use`
and its `tool_result`, and the provider rejects the whole turn. At the top of an
iteration every call already has its result.

Each line is prefixed with `OPERATOR_MESSAGE_PREFIX` (`Message from the operator
watching this run:`). Unlabelled, the agent reads it as a restatement of its own
task; labelled, it can weigh it as a live instruction.

Delivery is queue-and-consume, not pause-to-talk, so it is never promised:

- The echo goes through `session.events.info(...)`, i.e. the agent's own
  `QuietSink` buffer, so the line shows up inline in the attached stream *and*
  in the replay after a detach — not through `console.print`, which would leave
  no trace.
- An agent on its last iteration, or already reporting, never drains. When a run
  finishes with a non-empty inbox, `AgentSupervisor._finish` writes one line into
  its buffer saying how many messages went unread. Silently dropping what the
  user typed is the worst of the options.
- **There is no prompt while the master's turn is running.** `KerkovCLI.run()` is
  a `get_line` → `execute` loop, so a `Ctrl-O` attach mid-turn is watch-only
  until that turn ends; then you type at the next prompt. A second concurrent
  input surface during a turn would re-open the stdin-ownership hazard the
  mid-turn switcher closed, and belongs with the real prompt surface (#105).

Master and web sessions pass no `inbox`, so `LoopSession.inbox is None` leaves
both paths exactly as they were.

### Stopping one agent

`/agents stop <name|id>` asks a single agent to stop and report what it already
has, rather than killing it. Bare `/agents stop` is an error, not "stop
everything": mass cancel already has a name (`/q`) and an accidental one is
unrecoverable. If the stopped agent is the focused one, the view detaches first.

`AgentSupervisor.cancel()` / `cancel_all()` set the session's `EventInterrupt`
and give it `CANCEL_GRACE_SECONDS` (2.0) to unwind before falling back to
`task_handle.cancel()`. `cancel_all` shares **one** window across every agent,
so quitting does not scale its wait with the number running. The synchronous
`request_cancel` / `request_cancel_all` stay hard cancels — their callers (the
web session's `close()`, a cancelled parent tearing down its children) have no
chance to await a grace window, and an interrupt-only request there would orphan
the tasks.

A graceful stop only works because the loop honours the interrupt, and that took
a flag rather than the handle alone. `_handle_tool_calls` **absorbs**
`InterruptedByUser`: it resets the handle, records a tool result so history stays
well-formed, and continues to the next iteration. That is right for the master —
Escape means "stop this tool, keep the turn" — and wrong for an agent, which has
no operator to say "carry on". So `LoopSession.interrupt_ends_turn` (default
`False`; agents pass `True`) makes an interrupt terminal in both branches, and in
neither case is the handle reset — the runner reads
`interrupt_requested.is_set()` afterwards to tell a stopped run from a finished
one and builds a `partial` report with what the agent had, rather than `stuck`.
A `report` call that beat the cancel still wins: the agent's own findings beat
anything reconstructed from history.

A stop that lands *between* the calls of one batch is caught before the next
call starts: `_handle_tool_calls` re-checks `_stop_requested` on every
iteration and answers each remaining `call_id` with a skip result instead of
running it. The check has to be there rather than only at the loop top —
`run_cancellable` schedules the coroutine regardless, and a tool that finishes
without yielding (the synchronous file work inside `WriteTool.run`, say) wins
its race with the interrupt watcher and lands its side effect anyway.
`LoopSession.stop_exempt_tools` is the one exception, and `report` is its only
member: it writes nothing outside the run and is how a stopped agent hands back
what it found, so skipping it would throw away the findings the graceful stop
existed to preserve. The master's `stop_exempt_tools` is empty and
`_stop_requested` is false without `interrupt_ends_turn`, so Escape still runs
the whole batch and keeps the turn.

A **hard** cancel has to reach the tool too. `run_cancellable` owns the child
tasks it creates, and nothing else does: when its caller is cancelled it now
cancels and awaits both of them before re-raising. Without that, a
`request_cancel` landing mid-tool left the operation running detached — the
session read `CANCELLED` while the tool went on to land its side effect.

`AgentSession.cancel_requested` is what makes `_supervise` record `CANCELLED`
for a run that came back through the normal path with a partial report in hand.
The hard-cancel path (stub report, `CANCELLED`, and the `_settle_if_unfinished`
done-callback for a task cancelled before its first cycle) stays as the backstop.

**Cost to note:** a non-`None` interrupt routes every agent completion and every
agent tool call through `run_cancellable`, which creates two tasks and polls a
50 ms sleep for the duration. At `AGENT_MAX_CONCURRENT=5` that is noise, but it
is a change from a bare `await`.

While attached, the master's narration is **buffered, not printed**
(`BufferingSink` wraps the master's `ConsoleSink`). Two live conversations
writing to one terminal interleave line by line and neither is readable, so
`attach()` starts buffering and `detach()` replays everything in order under a
`— main conversation while you were away —` header. `status()` is the one
member that is never recorded: it is a live spinner, and a replayed one is a
stale line. `start_turn()` is passed straight through so the turn timer runs in
real time. The buffer is capped at `MAX_BUFFERED_EVENTS`; on overflow the
oldest are dropped and the flush says how many, the same shape as
`REPLAY_TAIL_LINES`. The buffer is only worth the call sites that go through
it, so agent-report notices and turn errors in `get_response` route through the
sink rather than `console.print`.

Two consequences worth knowing:

- `/clear` and `/resume` detach before settling agents, so focus never points
  at an agent belonging to the conversation that was just replaced.
- The picker is name-first (`● google-pinger  recon — ping 10.0.0.5 · exec ·
  00:42 · iter 6/30`); the id is still what the picker returns and what the
  model was told, so a row in the terminal is traceable to a report.

### Approving an agent's tool calls yourself

An agent auto-approves anything in the whitelist authorized by the delegation,
whether through a prompt, rule, or approval mode (`SupervisedAgentApproval`).
That makes detachment work: for most of a run nobody is watching, and a question nobody
can see is a hung agent, not a safety feature.

`/agents review <name|id> [on|off]` opts one agent out of that, and only while
you are **attached to it**. Three conditions, all in
`AttachedAgentReview.should_review`:

- the session's `review_tools` flag is on (`/agents review` sets it; bare
  `/agents review <name>` toggles),
- that agent is the focused one — a detached agent has nobody to ask, and only
  one agent is on screen at a time, so two can never compete for one answer,
- the run is interactive; a `--message` one-shot auto-approves everything and
  has no terminal to prompt on.

When all three hold, the call goes through the **master's** approval path —
`ToolApprovalCliHelper` with the current `/approval` mode — so deny rules, allow
rules and `yolo` behave exactly as they do for the conversation. Only the panel
title changes, naming the agent that is asking. `report({})` is auto-allowed in
`config/approvals.yaml`, so review does not interrupt an agent finishing.

The plumbing is one strategy and one gate. `SupervisedAgentApproval`
(`agents/agent_approval.py`) is what every runner is built with; it holds the
session and an optional `AgentReviewGate`, auto-approves when the gate declines,
and otherwise marks the session `BLOCKED_ON_INPUT` for the duration — the first
stage that reaches that state, and what `/agents` and the strip show while the
box is up. It restores the *previous* state rather than forcing `RUNNING`, so a
cancel landing while the box is open is not papered over. The gate itself lives
in the CLI (`cli/agent_review.py`) because "is this agent on screen, and can we
prompt right now" is a terminal question the agents package should not answer.

**Getting the question onto the screen is the hard part**, because the master's
chat prompt normally owns stdin and prompt_toolkit allows exactly one
application on it. `InputManager.takeover()` is the claim:

- `_terminal_free` is an `asyncio.Event`, set only while neither the agent
  picker nor a takeover holds stdin. `begin_switcher` clears it too, so the two
  wait for each other instead of opening two applications at once.
- A live chat prompt is asked to step aside: its typed text is stashed into
  `_pending_prompt_text`, the app exits with `TAKEOVER_SENTINEL`, and the
  claimant waits for `app.is_running` to go false before starting its own.
  `get_line` swallows that sentinel, waits out the claim, and re-prompts with
  the stashed text restored, so the sentinel never reaches a caller and nothing
  half-written is lost.
- `start_watching_for_interrupt` is inert during a takeover, the same way it is
  during the picker: a second cbreak reader on the same fd wedges the terminal
  rather than raising.
- `ToolApprovalCliHelper` holds an `asyncio.Lock` around the box itself. The
  master's turn and an attached agent prompt through the *same* transient
  prompt_toolkit session, so without it a master approval and an agent approval
  could land together. The master's own approvals do not take over the terminal
  — during its turn there is no chat prompt to displace — they just queue on
  that lock.

Two limits worth stating. Transient prompts deliberately skip the
`wait_for_terminal` check, because they are how a claimant asks its question and
waiting there would be waiting on its own claim. And the box is modal: while it
is up you answer 1/2/3 (or Ctrl-D, which denies) — `/back` and `/agents stop`
are not reachable until it is answered.

### The strip under the prompt

`agent_strip` (`cli/agent_display.py`) renders one line under the chat prompt:
`main · ● port-scan exec · ● tomcat-enum thinking · ○ cve-check done`, with the
focused entry emphasised. It is supplied to `InputManager` as a callback
(`set_status_provider`) so that module stays agent-agnostic, and it is built
from style/text fragments rather than markup — an agent's name is chosen by the
model and must never be readable as formatting.

It is **absent** when there is nothing to say: a permanent bar would spend a
terminal line telling every user without agents that they have none. The
toolbar is therefore assigned to the session rather than passed to
`prompt_async`, which only ever *sets* the attribute — passed once, it could
never be taken away when the last agent finishes. Chips cover active agents
plus finished ones whose report has not been delivered yet, active first, and
past `STRIP_MAX_CHIPS` the rest are counted (`+3 more`) instead of wrapping.

There is no strip during a turn, because there is no prompt during a turn. The
mid-turn answer stays `Ctrl-O`.

## Finding an agent run afterwards

`/agents` only knows about this process: its sessions and their `QuietSink`
buffers die with the CLI. The transcripts do not — every agent run writes a
`kind="agent"` engagement file linked to its parent — so discovery has a second
half on disk.

- `/engagements` lists conversations and says how many agent sub-runs it hid.
  The filter is applied inside `EngagementStore.list_engagements(kind=…)`,
  before the limit: filtering after it meant a directory of agent runs could
  push every conversation out of a ten-row table.
- `/engagements agents` lists the sub-runs themselves — id, profile, parent
  engagement, message count, and the task instruction recorded at spawn.
- `/resume <agent-engagement-id>` opens that run **read-only**
  (`cli/agent_transcript.py`) rather than restoring it. An agent engagement has
  `mode="agent"`, which is not a mode the CLI can switch into, and continuing
  one would detach it from the `report` its parent is waiting on. The check runs
  before `restore_engagement`, and the resume picker lists conversations only,
  so an unresumable id is never offered in the first place.

The transcript view prints the last `TRANSCRIPT_TAIL_MESSAGES` with each
message's role, clipping any single message at `MESSAGE_CHARS` and escaping the
body — a tool result full of brackets is data, not styling.

## Shared run workspace

Every agent in an engagement tree, and the master, share **one** workspace
directory keyed on the **root** engagement id (`resolve_root_engagement_id`,
the same rule the knowledge base uses for its store). The supervisor resolves
and creates it once at first spawn and injects the path into every
`AgentRunner`; the runner activates it instead of deriving a private directory
from its own engagement id, which nothing else could see. The path-traversal
guard in `RunWorkspaceManager.path_for_engagement` still applies.

**Collision caveat:** concurrent agents writing the same filename in that
directory will clobber each other. This is an accepted tradeoff — the point of
the shared directory is that `a1`'s output is readable by `a2` and by the
master. The mitigation is a prompt convention (agents namespace their outputs
by id or task), not enforced isolation.

## Container lifecycle delegation

Docker Compose startup, readiness checks, status, and shutdown are delegated to
the built-in `container-operator` profile. It uses `container_lifecycle` rather
than streaming Docker through `exec`. The lifecycle tool performs its polling
locally, writes complete pull/build/status output under
`artifacts/container-logs/` in the agent workspace, and returns one compact
result containing readiness, containers, ports, the artifact path, and cleanup
command. This keeps repetitive progress output out of both the agent and master
contexts.

## Defining your own agent

Drop an `AGENT.md` into `agents/<name>/AGENT.md`:

```yaml
---
name: my-agent
description: One-line summary the master LLM sees in the index.
allowed_tools:
  - read
  - http
max_iterations: 20
---

(System prompt body — the agent reads this verbatim.)
```

The `allowed_tools` list is the **maximum** tool whitelist for spawns of
this profile. A spawn call may narrow it but never widen it; names not in
the whitelist are rejected.

`recall` is subject to the same rule. Every built-in profile whitelists it,
so agents can read what the engagement already gathered instead of re-running
it; a user-defined profile that omits the name does not get the tool.

A user-defined profile may shadow a built-in (a warning is logged) — the
user opted in by writing it.

## Configuration

Env vars (see `src/kerkov/core/config_manager.py`):

| Key | Default | Purpose |
|---|---|---|
| `AGENT_MAX_DEPTH` | 2 | Master → agent → agent. At the cap, `delegate` is excluded from the agent's tool set. |
| `AGENT_MAX_TOTAL` | 20 | Hard ceiling on agents spawned in this process. |
| `AGENT_MAX_BATCH` | 5 | Largest task list one `delegate` call may carry. |
| `AGENT_MAX_CONCURRENT` | 5 | Live agents allowed at once. Counted at spawn time over `supervisor.active()`; finished agents free their slot. |
| `AGENT_MAX_ITERATIONS` | 30 | Process-wide cap on each agent loop; profile limits can be lower. |
| `AGENT_ROUTING_ENABLED` | false | Enable model routing for future delegated tasks at startup. |
| `AGENT_ROUTING_SMALL_MODEL` | empty | Model ID for eligible literal extraction and formatting tasks. |
| `AGENT_ROUTING_MEDIUM_MODEL` | empty | Model ID for eligible routine, benign read-only analysis. |
| `AGENT_ROUTING_LARGE_MODEL` | empty | Model ID for general, complex, security-sensitive, or uncertain delegated work. |

## Persistence

Agent runs write to the same `~/.kerkov/engagements/` directory as
conversations, with `kind="agent"` and back-links to the parent engagement
and parent `delegate` tool call. `/engagements` hides agent sub-runs by default
and says how many were elided; `/engagements agents` lists them with their
parent linkage, and `/resume <agent-id>` opens one read-only (see "Finding an
agent run afterwards"). Because agents are detached, a transcript may still be
growing when the master's turn ends.

Agent metadata includes the selected `model` and optional `provider`,
`routing_tier`, and `routing_reason`. The optional fields default to `None`
when loading files written before model routing was introduced.
