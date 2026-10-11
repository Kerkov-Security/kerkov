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

Assistant responses appear as text arrives, in a single message that is updated
with the completed answer. Providers that do not send text chunks still display
their completed response. Set `STREAM_OUTPUT=false` to wait for whole responses
in both the web console and CLI; delegated agents do not stream.

Stopping during generation keeps the text already received and saves it as a
partial assistant message in the engagement. The console labels unfinished text
**Partial response**, including when a provider fails or the connection closes.
Text left visible after a provider failure or disconnect is a preview and may
not have been saved. Streaming leaves your scroll position in place when you
are reading earlier messages.

## Stopping a run

While Kerkov is working on your message, **Stop** appears in the workspace
header. Select it to cancel the current model request, tool call, or pending approval. The button
shows **Stopping…** until cancellation finishes, then the composer becomes
available for your next message in the same engagement. Conversation history,
assets, findings, provider selection, and approval settings are preserved.
A model response that completes after Stop is accepted is discarded instead of
being added to the conversation as a finished answer.

Stop ends the main conversation turn. Delegated agents and background processes
that were already started keep running. Use **Stop agent** for an agent or the
[process tool](../reference/tools/exec-process.md) for a background process.
Completed actions are not undone. Changing mode and clearing a conversation
are separate actions and do not offer Stop.

## Saved engagements

The **Engagements** section in the conversation sidebar lists saved conversations,
most recently updated first. It uses the same local storage as CLI engagements;
agent sub-runs are hidden. Search matches the session ID, summary (or first user
message when no summary exists), operating mode, and saved model. **Load more
sessions** pages through matching results. Changing the search starts again at
the first page and clears the previous results while the new results load.

Select a session to see its saved mode, approval setting, and model, then choose
**Resume engagement**. Resume restores the saved conversation, operating mode,
verbosity, approval setting, workspace, inventory, and findings. It keeps your
currently selected provider and model; it does not generate a response or rerun
tools just to open the conversation. Before switching, the console checks that the saved
history, tool schemas, and findings fit the active model with room for its reply.
If they do not fit, choose a larger-context model and retry; your current
conversation stays open and neither transcript is changed. With no active
provider, this check runs when you select a model instead. Token usage starts at
zero for the resumed browser session. Finished agents are not restarted, and old
session-only approval rules are cleared. Resuming a saved `yolo` engagement
requires `ALLOW_AUTONOMOUS=true`.

Recent messages appear first; **Load earlier messages** retrieves older messages
from the transcript as it was opened. Long individual messages have explicitly
clipped previews, while full messages and provider replay data remain in the
saved engagement and model history. Conversations above 20 MiB must be opened
in the CLI. Existing compaction summaries are preserved; resume cannot recover
messages previously replaced by compaction.

Use **New engagement** to start fresh without deleting the previous conversation.
The new conversation is saved when you send a message or save an inventory item.
Send or clear message drafts and save or cancel resource edits before
switching. Switching waits for chat, provider changes, and active agents to finish;
a failed load keeps the current conversation. The same engagement cannot be
opened by two browser connections to this server at once. This guard is local to
the server process: avoid editing the same engagement simultaneously through the
CLI or another server process.

If the WebSocket disconnects, the visible transcript stays readable. Reload the
page and resume the saved engagement to continue.

## Providers and models

Use the provider and model selector beside the message composer to choose a
configured provider, search its available models, and apply a selection to the
next message. The console uses the same saved credentials and configured
endpoints as the CLI. Providers that still need credentials are marked as
unconfigured; open **Manage providers** in the selector to set them up.

Enter an API key for Anthropic, Gemini, NVIDIA, OpenRouter, or DeepSeek, or set
the HTTP(S) server address for Ollama, including a reverse-proxy path prefix
such as `https://host/ollama`. Leaving the key field empty keeps the
existing credential; saving with a blank key field does not copy an environment
key into persistent storage. A session-only key can still be saved without
retyping it. **Test connection** checks the submitted settings without
saving them. **Save setup** validates before saving; use the model picker
afterward to activate a model. A failed test or save keeps the current model.
Connection checks can make a small model request and consume provider tokens.

**Save for future sessions** stores keys through the shared protected credential
store (OS keyring or the private local-file fallback), and stores the Ollama
address in user settings. If credential persistence is unavailable, the console
reports that the key is available only for the current browser connection.
Unsaved keys are isolated from other tabs and discarded on disconnect. Keys are
never returned to the browser or placed in browser storage. Codex uses an existing
saved sign-in; complete sign-in through `kerkov` before testing it here.

Switching keeps the conversation, assets, findings, token usage, and completed
agent reports. Kerkov validates the selected model before activating it and
updates the conversation's context budget. A failed or timed-out request leaves
the current provider and model active; use **Retry** to reload a failed catalog.
When history is too large for a smaller model, the current provider summarizes
older messages before the switch is applied. This can use model tokens and has
a separate three-minute timeout. Errors identify when the current provider
cannot summarize the history. If the
remaining history cannot fit, the switch is rejected and the original history
stays active. Tool exchanges are translated for the selected provider.
Only models returned by the provider can be selected. Use **Refresh** beside
Model to reload the catalog if a model is added or becomes unavailable.
Connection validation can make a small model request and consume provider
tokens; these checks do not send conversation history.

Model switches apply to this browser session. Use **Save as default** for the
selected provider and model to use them in future sessions, including the CLI.
The selected setup must also be available in future sessions. If its key or
Ollama address is temporary, first use **Save setup** with **Save for future
sessions** enabled, then save the default. A failed setup save leaves the
previous default unchanged.
Explicit environment or `.env` values still take precedence over saved defaults;
the console shows a warning when those values override the saved choice. If
your `.env` was copied from an older template, remove or comment out its
`LLM_PROVIDER` and `DEFAULT_*_MODEL` lines to use saved defaults.

Switching and saving provider changes are unavailable while a message, inventory
save, or agent is working. Wait for a provider change to finish before sending
another message or changing mode.

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

## Network map

The **Network map** tab groups saved URLs and domains by their exact normalized
hostname. IP-literal URLs share a card with that exact IP. Different hostnames
stay separate even when their saved DNS answers share an IP. A DNS-only IP is
shown inside its host's details; IPs with access routes or other relationships
retain their own cards. Repositories appear in a compact list and in related
hosts' details instead of adding more nodes to the canvas.

Arrows show only explicit **reaches** connections, from their recorded source
to a destination host. Multiple services between the same directed pair share
a line; select it to see each protocol, port, service, observation timestamp,
and evidence reference. Reverse connections remain separate. A solid line
means all bundled services have observed evidence. Dashed lines contain at
least one declared, unverified service; the inspector shows each service's
individual status. Observations describe their recorded time, not live access.

**View from** selects a recorded origin, including explicitly named runners,
and shows only that origin's direct outgoing routes. **All origins** is the
default. The map does not infer transit paths, authenticated access, or routes
from DNS and deployment relationships. **No recorded route** means no incoming
access evidence is saved. With filters active, **No matching route** means no
incoming route matches those filters; other routes may be recorded. Neither
status means the host is unreachable. Record
access through the [ledger tool's connections section](../reference/tools/ledger.md).

Search by hostname, URL, saved name, DNS address, or associated repository.
Role and type filters match the resources inside each host. Direct connection
sources remain visible as context when their destinations match a filter.
Only routes to matching destinations are shown; keeping a source visible does
not show reverse routes to that source unless it also matches the filter.
Card status and service counts use visible incoming routes, and the inspector
lists visible incoming and outgoing routes. The header shows engagement totals;
the footer shows the visible hosts and routes.
Select a host to inspect its saved resources, DNS/deployment evidence, and related repositories. Use
**Open URL** to open a saved HTTP(S) endpoint or its inventory action to inspect
the original record. Addresses whose host the browser would reinterpret, such
as `https://127.1/`, remain visible without an **Open URL** link.

Drag the background to pan, scroll or use **+ / −** to zoom, and use **Fit** to
see the whole map. The canvas supports arrow keys to pan, **+ / −** to zoom,
and **0** to fit. Tab through hosts and routes and press Enter or Space to
inspect them. Escape clears the selection.

The map refreshes with inventory updates from the conversation and its agents.
**Refresh** requests the latest saved relationships. Clearing the conversation
or changing mode clears the map; after a disconnect, the last received map
remains readable. The map requires the engagement knowledge base to be available.

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

Expand an agent and choose **Inspect output** to follow its recent conversation
and tool results. The panel also shows recent activity, including tool calls and
status messages. It refreshes while open and marks clipped output; inspecting
an agent does not consume its report or change the main conversation.

Use **Send instructions** to queue a message for that agent at its next iteration.
The confirmation means queued, not read: if the run finishes first, its activity
records how many messages went unread. **Stop agent** requests a graceful stop,
allowing a partial report before cancellation if needed. These controls work
while the main conversation is busy and leave sibling agents running. Stopping
an agent also cancels any child runs it is waiting on.
Finished or stopping agents cannot receive new instructions. On disconnect, the
last received output remains readable and controls are disabled.

Reviewing individual agent tool calls before execution still requires the CLI
(see the [agent subsystem](../concepts/agent-subsystem.md)).

## Configuration

Web-specific variables are listed in the
[configuration reference](../reference/configuration.md#web-console-kerkov-web):
`KERKOV_WEB_SECRET_KEY`, `KERKOV_WEB_ACCESS_TOKEN`, `KERKOV_WEB_ALLOWED_HOSTS`,
and `KERKOV_WEB_DEBUG`. The runtime boundary and threat model are described in
[Web architecture](../concepts/web-architecture.md).
