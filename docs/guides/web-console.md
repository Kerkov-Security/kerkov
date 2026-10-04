# Web Console

Kerkov ships a browser-based console over the same runtime as the CLI. It is a
**proof of concept**, not a second product — it exists to show the runtime is
front-end agnostic. The CLI is the primary experience.

!!! warning "Alpha, unauthenticated, loopback only"
    The console has no user authentication and intentionally binds to localhost
    only. Do not publish it to a LAN, tunnel, or reverse proxy.

## Running it

The console needs the `web` extra:

```bash
uv sync --locked --extra web
uv run --no-sync kerkov-web runserver
```

Then open `http://127.0.0.1:8764/`. It reads the same `.env` and credentials as
the CLI. To bind a different loopback port:

```bash
uv run --no-sync kerkov-web runserver 127.0.0.1:9000
```

Binding to any non-loopback host is refused by design. Access is guarded by
`KERKOV_WEB_ACCESS_TOKEN`; if you don't set one, a fresh token is generated and
printed at startup.

## What works

The console runs the full conversation loop over WebSockets: sending messages,
receiving responses, tool execution, and interactive tool **approvals**. It
opens the knowledge base and findings ledger the same way the CLI does, so
[recall and the ledger](memory.md) work here too.

## Providers and models

Use the provider and model selector beside the message composer to choose a
configured provider, search its available models, and apply a selection to the
next message. The console uses the same saved credentials and configured
endpoints as the CLI. Providers that still need credentials are marked as
unconfigured; set them up with `kerkov` and reopen the console. Credential entry
and browser sign-in are not available in this selector.

Switching keeps the conversation, assets, findings, token usage, and completed
agent reports. Kerkov validates the selected model before activating it and
updates the conversation's context budget. A failed or timed-out request leaves
the current provider and model active; use **Retry** to reload a failed catalog.
Only models returned by the provider can be selected. Use **Refresh** beside
Model to reload the catalog if a model is added or becomes unavailable.

Changes apply to this browser session. Switching is unavailable while a message,
an inventory save, or an agent is working. During a switch, wait for validation
to finish before sending another message or changing mode. The startup defaults
are unchanged when you open another session.

## Assets and targets

Open the **Assets & targets** tab beside Conversation, or use the inventory
summary below Operating Mode. Assets are resources you protect; targets are
subjects of an offensive assessment. These roles provide context and do not
change operating mode or tool approvals.

Add or edit a domain, IP address, HTTP(S) URL, or absolute local repository path,
with an optional name. Records use the same [ledger](../reference/tools/ledger.md)
as the CLI and agents. Saving the same type and value in the same role updates
the existing record; the same resource can appear independently in both roles.
Saving does not call the model. The first save starts an engagement if needed;
opening an empty inventory does not.

Select a record to inspect its details and preview any linked evidence.
**Use in conversation** puts the resource into your message draft so you can
review it before sending. The inventory refreshes as the conversation and its
agents work.

Clearing the conversation or changing operating mode starts a fresh inventory;
the previous records remain in their saved engagement. If the connection closes,
the last received inventory remains visible but cannot be edited. Inventory is
unavailable when `KB_ENABLED=false` or the engagement knowledge base cannot open.

## Following agents

The **Agents** section below Session Context lists agents delegated in the
current conversation. Each row shows the agent's name, live state, and current
tool. Expand a row to see its task summary and iteration count. When a report
arrives, **Open report** shows its findings and next steps in a wider panel below
the conversation. Opening a report does not consume it or prevent delivery to
the main conversation.

Clearing the conversation or changing operating mode settles its agents and
clears the panel. If the browser disconnects, active rows are marked
disconnected; previously received reports remain readable on that page.

Attaching to an agent's full output, sending it messages, reviewing its tool
calls, and stopping an individual agent still require the CLI (see the
[agent subsystem](../concepts/agent-subsystem.md)).

## Configuration

Web-specific variables are listed in the
[configuration reference](../reference/configuration.md#web-console-kerkov-web):
`KERKOV_WEB_SECRET_KEY`, `KERKOV_WEB_ACCESS_TOKEN`, `KERKOV_WEB_ALLOWED_HOSTS`,
and `KERKOV_WEB_DEBUG`. The runtime boundary and threat model are described in
[Web architecture](../concepts/web-architecture.md).
