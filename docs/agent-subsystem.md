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
