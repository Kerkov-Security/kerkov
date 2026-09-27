# Agents

When a task branches — several things to enumerate at once — or when a sub-task
would bury the conversation in noise you do not need to read, Kerkov can hand it to
an **agent**: an isolated run with its own restricted tool set that works in the
background and reports back a short structured result. The scan's ten thousand
404s stay out of your main conversation; its one useful line comes back.

This guide is about running and watching agents. For the tools the model calls, see
[`delegate` and `agent_wait`](../reference/tools/delegate.md); for defining your
own agent, [agent profiles](../reference/agent-profiles.md); for how it all works
underneath, the [agent subsystem concept page](../concepts/agent-subsystem.md).

## How delegation looks from your seat

1. The model decides to delegate and asks to spawn one or more agents. With the
   packaged rules, **you approve the batch** in `strict` or `normal` mode.
   `permissive` and `yolo` auto-approve it; saved allow rules also apply in `normal`.
   Allowing a batch grants its agents permission to run their whitelisted tools
   without asking you again. See
   [delegation approval](../reference/tools/delegate.md#approval).
2. The agents start immediately and run in the background. The model keeps its
   turn; you keep your prompt.
3. When an agent finishes, its report comes back — either because the model waited
   for it, or delivered automatically at the next turn. You do not have to do
   anything to receive it.

A report says what the agent found, whether it `completed`, stopped `partial`, or
got `stuck`, and how many tokens it cost. Agents do not share the master's token
budget, so their report and the `/agents` list are the only places their spend
shows up.

## Watching agents: `/agents` and `Ctrl-O`

`/agents` lists every agent this session has spawned — running and finished — and
opens a picker to switch which one the terminal is pointed at.

| Command | What it does |
|---|---|
| `/agents` | List agents and open the picker. |
| `/agents <name\|id>` | Attach to that agent directly. |
| `/agents master` or `/back` | Return to the main conversation. |
| `/agents stop <name\|id>` | Ask one agent to stop and report what it has. |
| `/agents review <name\|id> [on\|off]` | Toggle reviewing that agent's tool calls (while attached). |

`Ctrl-O` opens the picker without typing `/agents`, and — importantly — it works
**mid-turn** as well as at the prompt. An agent is most interesting while the
master is still working, so you can peek at one without waiting for the turn to
end. Anything you had typed is stashed and restored afterwards, and Esc cancels the
picker without changing anything. (Mid-turn `Ctrl-O` needs a POSIX terminal; on
Windows it works only at the prompt.)

Each agent has two handles. The **id** (`a1`, `a2`) is its stable internal address;
the **name** (`tomcat-enum`) is a label the model picked, and what you mostly see.
Both resolve wherever a command takes an agent.

## Attaching to an agent

Attaching shows you that agent's output: the tail of what it has already done,
then its work live as it happens. Attach to a finished agent and you see the same
recorded output, marked finished.

While you are attached, the master's own narration is held back and replayed in
order when you detach, under a header — so the two conversations never scramble
together on screen.

## Talking to an agent you are attached to

Type an ordinary message while attached and it goes **to that agent**, not the
master — you are looking at the agent, so you are addressing it. Slash commands
still run, so `/back` and `/q` always work.

The agent reads what you type as a live instruction from the operator watching it,
distinct from its original task. A few things to know:

- Your message is queued and consumed at the agent's next step, not injected
  instantly — it is delivered, not a pause-to-talk.
- An agent that is already on its last step, or already reporting, will not read it.
  If a run finishes with unread messages, you are told how many went unread rather
  than having them silently vanish.
- **There is no prompt while the master's turn is running.** A mid-turn `Ctrl-O`
  attach is watch-only until that turn ends; then you type at the next prompt.

## Stopping one agent

`/agents stop <name|id>` asks a single agent to wind down gracefully and report
what it already found, rather than killing it outright. It gets a couple of seconds
to stop cleanly before being cancelled hard. A stopped agent comes back as
`partial` with its findings so far — work in progress is preserved, not thrown
away.

Bare `/agents stop` with no target is an error, deliberately: stopping everything
already has a name (`/q`), and an accidental "stop all" is not something you can
undo.

## Reviewing an agent's tool calls

By default an agent auto-approves any tool in the whitelist authorized by the
delegation, whether through a prompt, rule, or approval mode. That lets it run
unattended — a question nobody is watching for is just a hung agent.

`/agents review <name|id> on` opts one agent out of that, **while you are attached
to it**. Its tool calls then prompt you through the normal approval box, honouring
your current [`/approval` mode](approvals.md) and rules exactly as your own calls
do; only the box's title changes to name the agent asking. Turn it back off with
`/agents review <name|id> off`, or just detach.

Review only works on the agent you are attached to — a detached agent has nobody to
answer its prompt, and only one agent is ever on screen, so there is never a
question about which one is asking. The box is modal: while it is up, answer it
before other commands run.

## The strip under the prompt

When agents are active, a single line under your prompt shows them at a glance:

```text
main · ● port-scan exec · ● tomcat-enum thinking · ○ cve-check done
```

The focused one is emphasised, `●` is running and `○` is finished-but-not-yet-read.
It disappears entirely when no agents are active and no report is waiting — it never
occupies a line just to tell you there is nothing to show.

## Finding an agent run after it is over

`/agents` only knows about the current session; its list dies with the CLI. The
transcripts do not — every agent run is saved.

- `/engagements` lists your conversations and notes how many agent sub-runs it hid.
- `/engagements agents` lists the sub-runs themselves: id, profile, which
  conversation spawned it, and the task it was given.
- `/resume <agent-engagement-id>` opens that run **read-only**. An agent run cannot
  be resumed and continued — it belongs to the report its parent was waiting on — so
  the resume picker never offers one; you open it by id to read the transcript.

To go deeper than a report's summary, pass the agent's engagement id (printed with
its report) to [`recall`](../reference/tools/recall.md)'s `engagement` filter and
read the raw tool output behind the findings.

## Defining your own agent

Drop an `AGENT.md` into `agents/<name>/AGENT.md`:

```markdown
---
name: my-agent
description: One-line summary the master reads when choosing what to delegate.
allowed_tools:
  - read
  - http
max_iterations: 20
---

The system prompt — the agent reads this verbatim as its instructions.
```

`allowed_tools` is the **most** the agent may ever use; a delegation can ask for
less but never more. See [agent profiles](../reference/agent-profiles.md) for the
full schema, the five built-in profiles, and the loading rules.

## Configuration

Agent limits are set with environment variables — see
[Configuration](../reference/configuration.md#agents) for the full table:

| Variable | Default | Meaning |
|---|---|---|
| `AGENT_MAX_DEPTH` | `2` | How deep delegation may nest (master → agent → agent). |
| `AGENT_MAX_TOTAL` | `20` | Total agents allowed in one session. |
| `AGENT_MAX_BATCH` | `5` | Agents one delegation may spawn at once. |
| `AGENT_MAX_CONCURRENT` | `5` | Agents allowed to run at the same time. |
| `AGENT_MAX_ITERATIONS` | `30` | Steps an agent may take before it must stop. |
| `AGENT_TEMPERATURE` | _(unset)_ | Sampling temperature for agents. |

## Related

- [`delegate` and `agent_wait`](../reference/tools/delegate.md) — the tools behind
  delegation.
- [Agent profiles](../reference/agent-profiles.md) — built-in profiles and the
  `AGENT.md` schema.
- [Agent subsystem](../concepts/agent-subsystem.md) — how detachment, supervision,
  and delivery actually work.
- [Approvals](approvals.md) — the modes and rules that govern review prompts.
