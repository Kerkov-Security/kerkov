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
streaming responses, tool execution, and interactive tool **approvals**. It
opens the knowledge base and findings ledger the same way the CLI does, so
[recall and the ledger](memory.md) work here too.

## The gap: agents

A web session can *run* delegated agents — `delegate`, report collection, and
turn-boundary delivery all work — but there is **no attach, focus, review, or
per-agent stop UI**. A delegating web session is observable only through the
agents' final reports. Anything that needs to watch or steer an agent mid-run
belongs in the CLI (see the [agent subsystem](../agent-subsystem.md)).

## Configuration

Web-specific variables are listed in the
[configuration reference](../reference/configuration.md#web-console-kerkov-web):
`KERKOV_WEB_SECRET_KEY`, `KERKOV_WEB_ACCESS_TOKEN`, `KERKOV_WEB_ALLOWED_HOSTS`,
and `KERKOV_WEB_DEBUG`. The runtime boundary and threat model are described in
[Web architecture](../concepts/web-architecture.md).
