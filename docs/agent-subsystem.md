# Agent Subsystem

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
| `src/kerkov/agents/agent_wait_tool.py` | `agent_wait` tool — blocks until named (or all outstanding) agents report. |
| `src/kerkov/agents/report_format.py` | The single report formatter shared by `agent_wait` and turn-boundary delivery. |
| `src/kerkov/cli/agent_focus_manager.py` | `AgentFocusManager` — whether the terminal is pointed at the master or at one agent. |
| `src/kerkov/cli/agent_attach.py` | `AgentAttachView` — replays a focused agent's output, then follows it live. |
| `src/kerkov/cli/buffering_sink.py` | `BufferingSink` — records the master's narration while attached, replays it on detach. |
| `src/kerkov/cli/agent_display.py` | One rendering of a session, shared by `/agents`, the picker, and the attached prompt. |
| `src/kerkov/cli/messages/agents_message.py` | `/agents` and `/back` handlers. |

## How a delegation flows

1. Master LLM emits a `delegate` tool call with a list of `AgentTaskSpec`s.
2. `SpawnAgentTool` (REQUIRED approval) shows the user the batch — profile,
   instruction, effective tools per task — and asks for one approval.
3. On approval, `SpawnAgentTool.run()` turns each task into an
   `AgentSpawnSpec` and hands the batch to `AgentSupervisor.spawn()`. The spec
   carries a `build_runner` closure, because resolving a profile into a tool
   set (and reconstructing the nested `delegate` at the child's depth) is the
   tool's job while lifecycle is the supervisor's.
4. `AgentSupervisor.spawn()`:
   - Checks depth + total caps via `AgentRegistry`, then the live
     `AGENT_MAX_CONCURRENT` gate (all raise `BudgetExceeded`).
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
   - Runs the shared `ConversationEngine` with the session's `AgentEventSink`
     (a `QuietSink` that also keeps `iteration` / `current_tool` / state
     current) and `WhitelistAutoApproval`.
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
   Both paths render through `agents/report_format.py`, so the model only ever
   learns one shape. Each agent's `engagement_id` rides along so the user (or
   `recall`) can open the full transcript.
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

- A key pressed in the gap *between* awaits is dropped: nobody owns stdin
  there. This is pre-existing for Escape and not fixed here; fixing it means
  holding stdin across the whole turn, which is a re-architecture of
  `_await_interruptible`.
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

## Persistence

Agent runs write to the same `~/.kerkov/engagements/` directory as
conversations, with `kind="agent"` and back-links to the parent engagement
and parent `delegate` tool call. The
`/engagements` command hides agent sub-runs by default; the trailing
note shows how many were elided. Because agents are detached, a transcript may
still be growing when the master's turn ends.
