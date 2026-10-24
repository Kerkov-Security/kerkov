# Web Console

Kerkov ships a browser-based console over the same runtime as the CLI — the same
conversation engine, providers, tools, and approval layer, presented in a
browser instead of a terminal.

!!! warning "Unauthenticated, loopback only"
    The console has no user authentication and intentionally binds to localhost
    only. Do not publish it to a LAN, tunnel, or reverse proxy.

## Running it

The console needs the `web` extra:

```bash
uv sync --locked --extra web
uv run kerkov --interface web          # starts the server and opens a browser
uv run --no-sync kerkov-web runserver  # or start the server directly
```

`uv run kerkov` with no arguments and an interactive terminal asks whether to
start the CLI or the web console on first launch, and can remember your choice;
`--interface cli|web` overrides it. See the [CLI reference](../reference/cli.md)
for the flag and the `INTERFACE` preference.

Then open `http://127.0.0.1:8764/` (the `--interface web` path opens it for
you). It reads the same `.env` and credentials as the CLI. To bind a different
loopback port:

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

Completed responses are rendered as Markdown: headings, lists, tables, code
blocks and links. While a response streams, it shows as raw text. Links open in
a new tab. Raw HTML in a response is shown as text, and images are shown as
links rather than loaded, so text steered by a target cannot run scripts in the
console or send data out through an image request.

Stopping during generation keeps the text already received and saves it as a
partial assistant message in the engagement. The console labels unfinished text
**Partial response**, including when a provider fails or the connection closes.
Text left visible after a provider failure or disconnect is a preview and may
not have been saved. Streaming leaves your scroll position in place when you
are reading earlier messages.

## Connecting Hack The Box

Choose **Connect HTB** in the page header and paste an App Token from
[HTB profile settings](https://app.hackthebox.com/profile/settings). Select
**Verify and save** to check the token against HTB and save it in Kerkov's
protected credential store. HTB skills and future sessions use the same saved
token. The token is submitted outside chat and cleared from the password field.

Setup requires working persistent credential storage because HTB helpers run in
separate processes. If `HTB_API_TOKEN` is already set in the server environment,
update or remove that override and restart Kerkov before saving a different token.
Opening the dialog reports whether a token is configured; verifying it again
checks whether HTB still accepts it.

## Approval mode

The **Session Context** panel has an approval picker with **strict**,
**normal**, **permissive**, and **yolo**, the same modes as `/approval`. A
change applies to the next tool call, including during a run, so you can loosen
or tighten gating while Kerkov is working. Approval prompts that are already
waiting still need your answer. The new mode is saved with the engagement, so
resuming it restores the mode.

**yolo** runs every tool call without asking, including calls a deny rule would
block, and asks you to confirm before switching. It stays disabled unless
`ALLOW_AUTONOMOUS=true` is set when the console starts.

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

The **Engagement** tab lists saved conversations,
most recently updated first. It uses the same local storage as CLI engagements;
agent sub-runs are hidden. Search matches the session ID, summary (or first user
message when no summary exists), operating mode, and saved model. **Load more
sessions** pages through matching results. Changing the search starts again at
the first page and clears the previous results while the new results load.

Conversations saved before the separate verbosity and approval settings were
introduced also appear. Their legacy autonomy preset supplies those settings;
files with neither setting nor a preset use standard verbosity and normal
approvals. Opening or listing a conversation does not rewrite its transcript.

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

## Resource profiles

Select an asset or target and choose **Open resource profile**. The network map's
**Open asset profile** and **Open target profile** links open the same view.
Profiles are scoped to the current saved engagement and use the shared ledger;
opening one does not call the model, run a scan, or create a second database.

The header shows the resource address, its asset/target roles, the latest linked
record update, and the latest explicitly timestamped connection observation.
Unknown times remain unknown. One normalized resource identity has one profile,
including when it has separate asset and target records with different names,
environments, tags, or notes. **Edit this role’s details** edits that role's record.

- **Overview** shows saved context, host details, technologies when recorded,
  and counts of services and findings.
- **Services & connections** shows recorded ports, service versions, and
  incident graph connections. Follow a related resource link to open its profile.
  Declared connections remain labeled unverified; derived URL-host links are not
  connection tests.
- **Findings** shows severity, status, descriptions, remediation, and linked
  evidence when these fields have been recorded. Missing severity/status remain
  unknown. An empty list does not establish that a resource is secure.
- **Activity & evidence** shows current linked ledger records in order of last
  update, with creation/update times and evidence previews. It is not a scan log
  or a version history: overwritten or retracted entries are not retained here,
  and scans without linked ledger records do not appear.

Host details, services, and findings match an explicitly recorded `host`, `ip`,
or `hostname` to the resource's exact normalized address. A URL or repository
finding uses its complete URL or absolute path in `host`; URL paths and repository
paths remain case-sensitive when matching. A domain does not inherit an IP's
findings through DNS, and a URL does not inherit every finding on its host.
Unassigned findings stay in the ledger without being guessed into a profile.

**View evidence** previews only an artifact currently linked to a displayed
record or connection in this profile. Previews are plain text, limited to 32,000
characters. Sections show at most 200 records, indicate clipping, and retain full
counts; use the ledger and `recall` for the complete data. Profiles refresh with
inventory updates as the main conversation and agents record facts. After a
disconnect the last received profile stays readable with controls disabled.

**Investigate** appends the selected resource's context and its ledger profile
handle to your existing conversation draft. Its instruction is separate from
saved resource notes and asks the model to confirm the kind/value before
trusting the engagement-local ID, including if you switch engagements while
keeping the draft. Review and send the draft yourself.
The same profile is available to CLI conversations and agents through
`ledger` with `op="profile"` and an asset/target `entry_id`. No separate CLI
profile screen is needed.

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
Expand **More details (optional)** when adding or editing a resource to save an
**Environment** (128 characters), comma-separated **Tags** (up to 20 tags of 64
characters), and multiline **Notes** (4,096 characters). These details appear in
the inventory inspector and the map's saved resources. Clearing a field while
editing removes its saved value; the resource's address and role define its
identity independently of these details.

**Use in conversation** appends the resource's name, address, environment, tags,
and notes to your existing message draft so you can edit it before sending.
It does not send a message automatically. The inventory refreshes as the
conversation and its agents work.

Clearing the conversation or changing operating mode starts a fresh inventory;
the previous records remain in their saved engagement. If the connection closes,
the last received inventory remains visible but cannot be edited. Inventory is
unavailable when `KB_ENABLED=false` or the engagement knowledge base cannot open.

## Network map

Docker containers recorded by `container_lifecycle` appear as separate **Container**
cards, even when several labs publish ports on `127.0.0.1`. Confirmed access to a
published address, port, and protocol appears on the matching container card.
The route details retain the original host endpoint, evidence, and observation
time. Several containers sharing an IP stay distinct by their published ports;
ambiguous mappings leave the route on the host.

The shared host remains visible for unmatched services, unconfirmed port
mappings, or independent host context. When it only duplicates confirmed
container endpoints, the map hides its card and the redundant mapping lines.
Saved resources remain available in the container's details and inventory.
Choose **Containers** in the type filter to focus on labs; search also matches
their saved endpoint addresses.

Select a container to inspect its service, Compose scope, published TCP/UDP ports,
last-known running/stopped/removed state, readiness basis, timestamp, and log
artifact path. Saved endpoints link to their resource profiles for context,
findings, and **Investigate**.
Port mappings are not access routes and do not create observed-route arrows.
Docker state refreshes when the lifecycle tool runs startup, status, or shutdown;
changes made outside Kerkov require a new status call. Existing labs started
before this feature need a status call in their engagement to populate the map.


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
**Open URL** to open a saved HTTP(S) endpoint or its profile action to inspect
the original record. Addresses whose host the browser would reinterpret, such
as `https://127.1/`, remain visible without an **Open URL** link.

Drag a host, origin, or container card to rearrange the map. Curved connections,
arrowheads, and service labels follow the cards. Card positions survive inventory
refreshes and filter changes within the current engagement view; opening another
engagement restores the automatic layout.

Drag the background to pan, scroll or use **+ / −** to zoom, and use **Fit** to
see the whole map. The canvas supports arrow keys to pan, **+ / −** to zoom,
and **0** to fit. Tab through hosts and routes and press Enter or Space to
inspect them. Selecting a host or route keeps the rest of the map at full
opacity. Click the background or press Escape to clear the selection and restore
the default highlights and inspector. Panning preserves the selection.

**Add resource** stays available in the map toolbar, including on populated maps.

The map refreshes with inventory updates from the conversation and its agents.
**Refresh** requests the latest saved relationships. Clearing the conversation
or changing mode clears the map; after a disconnect, the last received map
remains readable. The map requires the engagement knowledge base to be available.

## Scripts

The **Scripts** tab beside Network map lists the files the engagement and its
agents have written into the run workspace
(`~/.kerkov/workspaces/<root-engagement-id>/`), most useful for reviewing the
scripts and proof-of-concept code generated while working a target. Files are
listed by path; select one to view its contents with language-aware syntax
highlighting and a line gutter, and use **Copy** to copy the source. Filter the
list by path with the search box.

The view is read-only — it never edits or runs anything. Binary files are listed
but not rendered, files are read up to the first 512 KB, and at most 1,000 files
are listed. The contents are whatever the tools wrote, so a file may contain
harvested secrets or raw tool output; treat it as working memory, not a
sanitized report.

The list refreshes when a run or agent turn finishes and when the workspace is
created; **Refresh** requests the current files. Clearing the conversation,
changing mode, and resuming a saved engagement all reset the view to that
engagement's workspace. Scripts appear only once the engagement has started.

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
