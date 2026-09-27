# Web Architecture

The Django web UI is a transport and presentation layer over the existing
Kerkov runtime. It should not shell out to `kerkov`, scrape Rich output, or fork
separate tool/provider implementations.

!!! warning "Proof of concept"
    The web console is a proof of concept, not a second product surface. Kerkov
    is built for the CLI; the console exists to prove that the runtime is
    genuinely front-end agnostic, and it is touched mainly to keep it from
    breaking.

    The practical gap is agents. A web session can *run* them — `delegate`,
    `agent_wait` and turn-boundary report delivery all work — but there is no
    attach, focus, review or per-agent stop UI, because those are terminal
    concepts (see [Agent subsystem](agent-subsystem.md)). A delegating web
    session is therefore observable only through the reports its agents file. Use
    the CLI for anything where watching an agent work matters.

## Browser Presentation

The console adopts the kerkov.me visual style through Django templates, local
CSS, and plain JavaScript. Its logo assets ship with the Python package. There
is no frontend framework, separate frontend service, or Node build step.
Django serves the page and static assets; Channels handles the existing
WebSocket event contract.

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
- `tool.start`, `tool.pending_approval`, `tool.result`
- `approval.request`, `approval.auto`
- `token.usage`
- `mode.info`, `mode.changed`
- `provider.error`
- `log.info`, `log.warning`, `log.error`
- `error`

Browser-to-server messages currently include:

- `user.message`
- `approval.response`

Server acknowledgements include:

- `user.message.accepted`, `user.message.rejected`, `user.message.complete`
- `approval.resolved`, `approval.rejected`

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

Web settings are loaded from `.env`. Provider and search credentials resolve
through the shared runtime credential manager, so a web session can reuse the
credentials previously saved by the CLI in the OS keyring or headless local-file
fallback. The browser does not yet provide its own credential-entry event or
modal:

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

The Playwright-backed `browser` tool is intentionally omitted from web sessions
until its browser processes and registry can be owned and closed per WebSocket.

## Scaling Notes

Keep new web features attached to runtime adapters rather than the Channels
consumer. A future database-backed Django product can add users, engagements,
reports, and audit logs without changing the core LLM/tool loop. A future
multi-client session can replace the in-memory `WebEventBus` with a channel
group or durable event stream while preserving the event contract.
