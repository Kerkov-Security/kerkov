# Web Architecture

The Django web UI is a transport and presentation layer over the existing
Kerkov runtime. It should not shell out to `kerkov`, scrape Rich output, or fork
separate tool/provider implementations.

!!! note "Shared runtime, not a parallel product"
    The web console is a thin presentation layer. It reuses the CLI's
    conversation engine, providers, tools, and approval layer rather than
    forking them, so runtime behavior stays consistent across both interfaces.

    The Agents sidebar shows live state, current tools, and completed reports.
    Agents also expose recent output, queued instructions, and individual stop
    controls. Reviewing individual agent tool calls before execution remains a
    CLI feature (see [Agent subsystem](agent-subsystem.md)).

## Browser Presentation

The console adopts the kerkov.me visual style through Django templates, local
CSS, and plain JavaScript. Its logo assets ship with the Python package. There
is no frontend framework, separate frontend service, or Node build step.
Django serves the page and static assets; Channels handles the existing
WebSocket event contract.

The workspace has Conversation and Assets & targets tabs. A summary below
Operating Mode links to the inventory, where assets and targets are grouped and
selectable for details, evidence previews, and editing. Use in conversation
fills the draft without sending it. Inventory refreshes follow conversation,
tool, and agent activity. Disconnecting leaves the last snapshot readable and
disables editing.

Agents appear below Session Context in the right sidebar. Rows expand for task
summaries and iteration counts; reports open in a wider panel below the
conversation. Status updates preserve the expanded row and keyboard focus.
The panel marks active agents disconnected when the socket closes, while keeping
received reports available for inspection.

## Runtime Boundary

- `kerkov.web.console` owns Django views, templates, static assets, and
  WebSocket transport.
- `kerkov.web.runtime` owns browser-facing runtime adapters:
  - `WebKerkovSession` wires config, provider, tools, skills, agents,
    workspaces, engagement persistence, context compaction, and
    `ConversationEngine`. It also opens the engagement knowledge base, builds
    the `FindingsLedger` over it, registers `recall` and `ledger` under
    `KB_ENABLED`, and drains finished agent reports at the turn boundary so a
    detached agent's findings reach the conversation even when nothing called
    `agent_wait`.
  - `WebInventory` reads and edits assets and targets in that same ledger and
    previews linked artifacts from the root engagement's knowledge base.
    Listing is lazy; the first save can start an engagement without a provider
    call. Resource validation is shared with the CLI and ledger tool.
  - `WebOutputSink` converts `ConversationEngine` callbacks into browser
    events.
  - `WebApprovalStrategy` keeps the same approval-rules model as the CLI, but
    prompts through the browser when a human decision is required.
- `kerkov.core.conversation_engine` remains the shared orchestration loop for
  CLI, agents, and web.

## Event Contract

Browser events use this envelope:

```json
{
  "type": "assistant.message",
  "payload": {},
  "timestamp": "2026-07-05T19:00:00+00:00"
}
```

Initial event families:

- `session.ready`, `session.cleared`
- `workspace.ready`
- `turn.start`, `turn.end`
- `status.start`, `status.end`
- `assistant.message`
- `assistant.stream.start`, `assistant.stream.delta`, `assistant.stream.end`
- `tool.start`, `tool.pending_approval`, `tool.result`
- `agent.updated`, `agents.reset`
- `agent.output`, `agent.message_sent`, `agent.stopped`, `agent.control_error`
- `inventory.snapshot`, `inventory.saved`, `inventory.evidence`, `inventory.profile`, `inventory.profile_evidence`, `inventory.error`
- `approval.request`, `approval.auto`
- `token.usage`
- `mode.info`, `mode.changed`
- `provider.error`
- `provider.catalog`, `provider.models`, `provider.changed`, `provider.selection_error`
- `provider.settings`, `provider.tested`, `provider.configured`, `provider.default_saved`
- `log.info`, `log.warning`, `log.error`
- `error`

Browser-to-server messages currently include:

- `user.message`
- `user.message.stop`
- `approval.response`
- `settings.approval` (the approval picker; bypasses the message lock so it
  applies mid-run, and is refused while an engagement is opening)
- `agent.inspect`, `agent.message`, `agent.stop`
- `inventory.list`, `inventory.save`, `inventory.evidence`, `inventory.profile`, `inventory.profile_evidence`
- `provider.catalog`, `provider.models`, `provider.switch`
- `provider.settings`, `provider.test`, `provider.configure`, `provider.default`

Server acknowledgements include:

- `user.message.accepted` (includes server-classified `stoppable`), `user.message.rejected`, `user.message.complete`
- `user.message.stopping`, `user.message.stop_rejected`
- `approval.resolved`, `approval.rejected`
- `approval.cancelled`

When `STREAM_OUTPUT` is enabled, each provider completion has a unique
`stream_id`. `assistant.stream.start` opens it, `assistant.stream.delta` carries
incremental `text`, and `assistant.stream.end` closes the delta phase. No empty
message is rendered for a stream without text. A successful `assistant.message`
carries the same ID and replaces the preview with authoritative completed text,
including tool-call narration; it never appends a duplicate. A provider without
deltas still produces one message. Non-streaming messages omit the ID.

An unfinalized preview is marked partial at turn end or disconnect. Stop uses
the shared engine's partial-message persistence. Failed provider requests and
disconnects leave visible previews without promising durable history. The
browser ignores deltas after stream end and responses for retired stream IDs.
Deltas stay plain text. Every `assistant.message` also carries `html`, the
completed text rendered on the server by `markdown-it-py` (CommonMark plus
tables and strikethrough, single newlines kept as line breaks), and the browser
swaps it in when the message completes. Resumed history rows for assistant
messages carry the same field, and its length counts toward the history page
budget; a row whose rendering would not fit even an empty page is sent as
plain text instead, so pagination always advances. Model text is partly
attacker-influenced through prompt injection, and the page also hosts the
approval tray, so the renderer escapes raw HTML, links only URLs that pass
`markdown-it`'s scheme check (no `javascript:`, `vbscript:`, `file:` or
`data:`), opens links in a new tab with `rel="noopener noreferrer"`, and does
not render images, which the browser would fetch unprompted. The browser uses
`innerHTML` only when a payload carries `html`; client-authored notices, user
messages, tool output and activity stay `textContent`.

`user.message.stop` carries the active `message_id`. The consumer reserves a
fresh interrupt handle before scheduling each message, so an early Stop cannot
miss the turn or cancel a later one. Stale IDs and commands such as `/mode` and
`/clear` are rejected. A matching request emits `user.message.stopping` and
settles pending approvals with `approval.cancelled`. The shared engine ends the
turn, records interrupted and skipped tool results, and awaits cancellation
cleanup before `user.message.complete` includes `stopped: true`. Only then can
the next message run. Each turn has a new handle, leaving the session, detached
agents, and previously started background processes available. Provider metadata
lookups and context compaction also honor Stop; disconnect retains its separate
session-wide teardown behavior.

Provider selection requests carry a `request_id`. `provider.catalog` returns
supported providers with canonical IDs, display labels, and configured flags,
plus the active provider and model. It never returns credentials.
`provider.models` takes a canonical `provider` ID and returns its model IDs and
the suggested `selected_model`. `provider.switch` takes `provider` and `model`;
success emits `provider.changed` with the active selection and provider metadata.
Failures emit `provider.selection_error` with `request_id`, `operation`, and a
safe display message. The browser ignores replies to superseded requests.

`WebProviders` owns this flow in the runtime adapter. It builds a detached
candidate from session configuration and existing credentials, checks model
membership and connectivity, and resolves context limits before committing the
selection. Switching holds the session lock and is refused during parent turns
or active agents. It preserves the engagement, inventory, and usage; oversized
history is summarized with the old provider before a smaller model is installed.
Codex candidates share the session's token manager, with serialized token
refresh, so catalog requests do not leave the active provider with stale auth.
Provider requests run in bounded background tasks so catalogs and validation
cannot block approval responses. Blocking provider work uses a separate pool
with four slots shared across sessions. A timed-out or cancelled request retains
its slot until the worker exits; additional work receives a retryable busy error
when all slots are occupied. The consumer reserves turn admission as soon
as a switch or settings mutation is scheduled and cancels pending provider tasks
on disconnect; late validation results cannot change a closed session.

Manage providers uses the same correlated request/error contract:

- `provider.settings` returns provider configuration flags, authentication kinds,
  safe endpoint metadata, and startup defaults. It never returns secret values.
- `provider.test` validates a detached candidate with an optional submitted
  `api_key` or Ollama `base_url`, returning models without saving or switching.
- `provider.configure` validates the same fields, then commits them with the
  requested `persist` preference and emits `provider.configured`. The active
  provider stays installed until a separate switch.
- `provider.default` validates a provider/model pair and writes both startup
  settings atomically, then emits `provider.default_saved`. It requires the
  validated key or Ollama endpoint to match the setup available to a future
  session; temporary or overridden setup is rejected before writing defaults.

Credential commits use the shared credential manager; settings use the private
user settings file. Validation runs in worker tasks without persistence. Custom OpenRouter endpoints
use a catalog-only startup check; explicit model selection and setup use the
one-token completion probe. The
final bounded credential read/write runs in the shared worker pool under the
session lock, so other sockets stay responsive. Once a commit has started,
cancellation drains it before releasing the lock; an OS write already in progress
cannot be rolled back. Disconnect clears session credentials again after the
worker exits and suppresses success responses. Settings metadata reloads saved
defaults on each request without changing the active provider. Environment
settings retain precedence over saved startup defaults. Codex reuses stored
authentication; this flow does not invoke its interactive OAuth setup.

Inventory requests carry a `request_id`. Saves and evidence requests also carry
the `context_id` from the current snapshot, so a request from a previous
conversation cannot operate on the new engagement. A snapshot contains
`context_id`, `engagement_id`, `available`, `detail`, and `entries`. Each entry
identifies its ledger row and role and contains the validated resource and
record metadata. Disabled or unavailable knowledge bases return an unavailable
snapshot.

`inventory.save` takes `section` (`assets` or `targets`), `resource` (`kind`,
`value`, optional `name`), and `entry_id` (null for an add). Edits can include
`expected_updated_at` to detect a stale record. A successful save emits
`inventory.saved` followed by `inventory.snapshot`. Edits replace the resource
fields, so omitting the name clears it. Changing a resource's identity drops
its previous evidence link; merging into an existing resource retains the
destination's evidence.

`inventory.evidence` takes the selected `entry_id` and previews only its linked
artifact, bounded to 32,000 characters. It does not search for inferred
associations or open arbitrary files. Failures use `inventory.error` with
`operation`, `detail`, `request_id`, and `context_id`.

`inventory.profile` accepts the selected resource `entry_id` with the current
`context_id` and a correlated `request_id`. The shared
`FindingsLedger.profile()` projects only resource, host, service, finding and
connection sections into combined roles, exact-address facts, graph links and
current-record activity. Credential/flag sections and artifact bodies are never
read for this projection. Sections are bounded, with counts and truncation flags.

`inventory.profile_evidence` additionally accepts `artifact_id`. The runtime
checks that the artifact is linked to a displayed record or connection in the
selected profile both before and after reading it. It returns at most 32,000
characters of plain text. Context retirement and disconnect suppress late reads;
the browser also correlates resource IDs and request IDs, discards superseded
replies, and clears profile state when engagements change. These are read-only
operations and remain available during a main conversation turn.

`agent.updated` contains an `agent` snapshot with `id`, `agent_run_id`, `name`,
`task` (the supervisor's task summary), `profile`, `state`, `cancel_requested`,
`current_tool`, `iteration`, `max_iterations`, and an optional `report` (`status`,
`findings`, `next_steps`, `tokens_used`). The web session subscribes to its
supervisor and emits only changed snapshots; raw agent history, tool arguments,
and buffered output are not included. Reading a report in the browser leaves
normal model delivery unchanged.

`WebAgents` handles `agent.inspect`, `agent.message`, and `agent.stop` with a
correlated `request_id`, `agent_id`, and `agent_run_id`. The run ID prevents a
stale control from targeting a reused agent ID after a reset. Inspection returns
bounded plain text from user, assistant, and tool messages, plus recent activity;
system prompts and raw provider blocks are omitted. Only the open output panel
polls. Messages enter the existing bounded inbox path and are consumed at the
next iteration boundary; acknowledgements mean queued, not delivered. Replayed
mutation request IDs are rejected. Stop uses the supervisor's graceful
cancellation and preserves report delivery. Controls remain available during a
master turn; stop tasks run separately so approval responses remain responsive.
Retirement and disconnect invalidate controls and discard late responses.

Clearing a conversation or changing mode settles the old agents, discards
pending reports, and replaces the supervisor and its delegate/wait tools before
emitting `agents.reset`. The lifetime spawn budget is retained. Retired
supervisors cannot publish updates into the new conversation. Disconnecting
unsubscribes the browser before cancelling its agents.

The reset also detaches the old knowledge base and emits an empty inventory
snapshot with a new `context_id`. Existing records remain persisted in the old
engagement. Asset and target roles are bookkeeping; tool approval rules continue
to govern execution.

## Security Boundary

The current web console is a single-operator, loopback-only development UI. It
has no user authentication and must not be bound to a LAN interface or exposed
through a reverse proxy. The ASGI stack requires the WebSocket `Origin` scheme,
host, and port to match the endpoint exactly, then applies the allowed-hosts
check. The dashboard supplies a random per-process access token in a host-only,
HttpOnly, SameSite cookie before a consumer creates the tool-capable runtime.
Keeping the token out of the WebSocket URL also keeps it out of Daphne access
logs. These controls reduce cross-site localhost attacks; they are not a
substitute for authentication in a hosted deployment.

Web settings are loaded from `.env`. Each WebSocket owns a credential manager
with a private session store, sharing only environment inputs and the persistent
backend. It can reuse credentials saved by the CLI in the OS keyring or headless
local-file fallback. Temporary credentials never cross browser connections and
are discarded on disconnect. Manage providers accepts secrets only in
access-controlled local WebSocket messages, clears password inputs on
close/disconnect or successful saving,
and never includes keys in results or browser storage. Candidate validation
errors are sanitized before reaching logs or browser events.

- `KERKOV_WEB_DEBUG` defaults to `0`; the local runserver still serves bundled
  static assets.
- `KERKOV_WEB_ALLOWED_HOSTS` defaults to loopback hosts and rejects any
  non-loopback value.
- `KERKOV_WEB_SECRET_KEY` and `KERKOV_WEB_ACCESS_TOKEN` default to random
  process-local values. A configured access token must contain only ASCII
  characters.

Each WebSocket owns its own process registry and supervisor. Disconnecting the
socket cancels its in-flight request, pending approvals, and owned subprocesses.
Independent tabs do not share conversation or process state.

The repo-local `data/` tree stores runtime artifacts, not general application
secrets. Provider and search keys belong in the selected credential store.
Environment variables remain a compatibility input for CI and unattended
deployments.

`WebBrowser` injects the existing Playwright-backed `browser` tool with a private
registry per WebSocket. The master and its agents share that tool. An async lock
serializes browser actions and preview capture, preserving the inherited
`execute()` validation wrapper. The main conversation uses `WebApprovalStrategy`;
custom agents whose profiles allow `browser` use `WhitelistAutoApproval`, so
their browser actions do not prompt the operator. No built-in profile includes
`browser`. Web launches are headless with a 1920 × 1080 maximum viewport.

The Browser tab sends `browser.preview` and `browser.action` requests while
visible, correlated by request ID and a rotating context token. The response contains session names,
URL/title, a viewport JPEG capped at 1 MiB, and the latest agent-requested snapshot
capped at 50,000 characters. Preview capture does not execute page JavaScript or
replace the agent's element refs. `browser.changed` invalidates the preview after
tool actions; it is not a continuous video stream. There is at most one browser
request task per connection. Preview requests time out after five seconds; operator
actions after forty seconds, including their refreshed preview. Approval
responses and Stop remain responsive. Target text uses `textContent`; only inline
JPEG data reaches the preview image, and target pages are never embedded as iframes.

`browser.action` requires the current context token and validates a bounded,
explicit action schema. The adapter handles operator control under the same lock
as tool calls. It reuses the existing browser tool for start, navigation, keys,
and close; pointer and keyboard input go through Playwright. Coordinate inputs
are normalized to the displayed viewport and require the latest frame token.
Manual changes clear cached snapshots and agent element refs. Agent changes
invalidate old frame tokens. The UI queues rapid typing and discards queued
input on errors, retirement, or disconnect. Operator actions do not enter the
conversation or tool-approval flow. The connection holds a single controlled
session ID; mutating tool calls against that session (including close-all) fail
until the operator returns control. Read-only inspection remains available.

An operator can start a fresh session or choose a named persistent Chromium
profile. The base browser tool exposes a profile-path hook whose default is
`None`, preserving fresh CLI and agent launches. The web tool uses the hook only
for an explicit persistent start from the panel. Profiles live under
`~/.kerkov/browser/profiles/`, outside engagement transcripts; the private-file
helper protects their directories. An OS file lock leases each profile through
browser cleanup, preventing concurrent opens across connections and processes.
Failed or cancelled starts release the lease after resource cleanup. Fresh
sessions never read or write these profiles.

A busy preview response carries `busy: true`. The client retains the last page
and waits for `browser.changed` before retrying, avoiding a request loop while an
action owns the lock. Other temporary errors and timeouts retain the last preview
with a status message. Session closure and context changes clear it.

Engagement retirement emits `browser.reset` before settling agents, drains the
preview/action lock, and closes all browser resources before opening the next
engagement. Cleanup runs even if agent reset fails or is cancelled, and the old
knowledge base is closed even if browser cleanup fails. Late previews from the
old context are discarded. Disconnect cancels the preview and main turn, settles
agents, and awaits browser cleanup. Existing sessions survive Stop but are never
restored from engagement persistence. Persistent profile data survives cleanup
and is reused only when the operator explicitly opens that profile again.

## Scaling Notes

Keep new web features attached to runtime adapters rather than the Channels
consumer. A future database-backed Django product can add users, engagements,
reports, and audit logs without changing the core LLM/tool loop. A future
multi-client session can replace the in-memory `WebEventBus` with a channel
group or durable event stream while preserving the event contract.
