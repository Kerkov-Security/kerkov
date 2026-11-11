# Engagements

An **engagement** is one saved conversation — its full history, the findings
ledger, and the captured tool output behind it. Engagements persist
automatically, so you can close Kerkov and pick up exactly where you left off.

## How they're stored

Each engagement is a JSONL transcript under `~/.kerkov/engagements/`, written as
you work. Alongside it, each engagement *tree* (a conversation plus any agents
it spawned) shares one SQLite knowledge base at
`~/.kerkov/engagements/<root-id>.kb.sqlite`. Everything is local; nothing is
uploaded.

## Resuming

From the shell:

```bash
# Resume the most recent engagement
kerkov --continue

# Resume a specific one by id
kerkov --resume <engagement-id>
```

From inside a session, [`/resume`](../reference/slash-commands.md#engagements)
with no argument opens a picker of your saved conversations; pass an id to
restore it directly:

```text
kerkov › /resume
kerkov › /resume abc123
```

When you exit with `/q`, Kerkov prints the exact resume command for the
engagement you were in.

## Listing

[`/engagements`](../reference/slash-commands.md#engagements) lists your saved
conversations. Add `agents` to see the agent sub-runs behind them instead, with
their parent linkage and task:

```text
kerkov › /engagements
kerkov › /engagements agents
```

## Agent sub-runs are read-only

A delegated agent gets its own engagement id, recorded under its parent. These
appear in `/engagements agents`, and you can open one to inspect it — but it
opens **read-only**: resuming an agent run and continuing it would detach it
from the report its parent is waiting on. Use it to review what an agent did,
not to keep it going. For live agents, see the
[agent subsystem](../concepts/agent-subsystem.md).

## What resuming restores

Restoring an engagement brings back its conversation history and, with the
knowledge base enabled, its findings ledger and recallable tool output. The
mode, provider, and model in effect are the session's current ones — resuming
does not force the old posture back on you.
