# MCP servers

Kerkov is an [MCP](https://modelcontextprotocol.io) client. Every tool an MCP
server advertises becomes a Kerkov tool named `mcp__<server>__<tool>`, approved,
executed, indexed into the knowledge base and persisted like any built-in tool.

Three transports are supported:

- **stdio** — a local command that Kerkov starts as a child process and talks to
  over standard input and output;
- **streamable HTTP** — a remote or local server at a URL, authenticated with
  a key sent in a header or with an OAuth sign-in;
- **HTTP+SSE** — the legacy remote transport (`"type": "sse"`), for servers that
  have not moved to streamable HTTP. Its URL is the server's SSE endpoint,
  usually ending in `/sse`. Headers, keys and OAuth work as they do for `http`.

## Install

MCP support is an optional extra:

```bash
uv sync --extra mcp            # in a checkout; or: pip install "kerkov[mcp]"
```

Without it, `/mcp` shows each server as failed with the install command.

## Configure servers

Servers live in `mcp.json`, in Claude Code's `mcpServers` shape, so an entry can
be copied between the two unchanged:

```json
{
  "mcpServers": {
    "burp": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@example/burp-mcp"],
      "env": { "BURP_API_KEY": "${BURP_API_KEY}" },
      "tools": { "deny": ["delete_*"] }
    },
    "intel": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": { "Authorization": "Bearer ${INTEL_MCP_KEY}" },
      "optional": ["INTEL_MCP_KEY"],
      "tools": { "allow": ["cve_lookup", "exploit_search"] }
    }
  }
}
```

Kerkov reads the first of:

1. `MCP_CONFIG_PATH`, when set. A relative value resolves against the Kerkov
   checkout and then `~/.kerkov`, never the working directory;
2. `config/mcp.json` in a source checkout (git-ignored; copy
   `config/mcp.example.json`);
3. `~/.kerkov/mcp.json`.

!!! danger "Never read from the working directory"
    Kerkov does not load `.mcp.json` or `mcp.json` from the directory it runs in.
    Operators run Kerkov inside cloned target repositories, and a planted file
    would start an attacker's command.

    For the same reason, a `.env` Kerkov finds in the working directory or its
    parents, other than the one at the root of the Kerkov checkout, cannot set
    `MCP_CONFIG_PATH` (it is ignored with a warning) and cannot fill MCP
    placeholders, for as long as the process runs. Export those in the shell or
    store them with `/auth set`.

| Key | Meaning |
|---|---|
| `type` | `stdio`, `http` or `sse`. Inferred from `command` or `url` when omitted; a `url` alone means `http`, so an SSE server needs `"type": "sse"`. |
| `url`, `headers` | The endpoint and extra request headers of an `http` or `sse` server. |
| `command`, `args` | The program to start and its arguments. |
| `env` | Extra environment for that server only. Kerkov's provider keys are never passed, even if listed. |
| `enabled` | `false` keeps the entry without connecting it. Default `true`. |
| `tools` | `{"allow": [...], "deny": [...]}` glob filters on the server's tool names, applied before advertising. Deny wins. |
| `optional` | Placeholder names that may be absent; see below. |
| `timeout` | Seconds to wait for one tool call. Default `300`. |
| `resources` | `false` withholds the server's [resource tool](#resources). Default `true`. |
| `trusted` | `true` lets allow rules, saved approvals and `permissive` mode apply to this server's tools. Default `false`. See [Trusted servers](#trusted-servers). |
| `oauth` | `http` and `sse` only. For a browser sign-in, all optional: `scope`, and `clientId` with `callbackPort` for a client registered in advance. For a machine-to-machine grant, `grant` and its credentials. See [OAuth](#oauth). |

Unknown keys produce a warning and are preserved when `/mcp add` or
`/mcp remove` rewrites the file. Server names are 1–32 letters, digits, `_` or
`-`, and may not contain `__`.

### Keys and placeholders

`${NAME}` and `${NAME:-default}` expand in `command`, `args`, `env`, `url`,
`headers` and the `oauth` block's `clientSecret`, `privateKey` and
`privateKeyFile`. Each name resolves from a session value, then the environment
(including Kerkov's own `.env`, but not one from a target's directory), then
Kerkov's credential store. Store one with `/mcp key <server> [NAME]`, which
prompts with masked input and reconnects the server, or with `/auth set NAME`.
The value never goes into `mcp.json`.

A placeholder is required unless it is listed in `optional`. A missing required
key keeps the server from starting. A missing optional key drops the header,
`env` entry or argument that uses it instead of sending an empty value, so a
server that also works anonymously gets no `Authorization: Bearer ` header. A
literal-looking secret in a key-like header or `env` entry produces a warning
suggesting a placeholder.

Prefer a header over a key in the `url`: a URL is sent in full to proxies and
server access logs. Kerkov keeps an MCP URL's userinfo, path and query out of
its own request and failure logs, and names any value it substituted into the
URL after the placeholder it came from, but it cannot keep them out of anyone
else's.

Connecting warns when the **resolved** endpoint sends headers over plain
`http://` to a host other than loopback. The check runs before the first
request, so a `url` that comes from a placeholder is covered too.

`/mcp` shows each server's auth state without ever showing a value:

| State | Meaning |
|---|---|
| `none` | The request carries no credential. |
| `key (<source>)` | Keys resolved; the source is `environment`, `session` or the credential store. |
| `key (config)` | The credential is written into `mcp.json` itself, as a literal or a `${NAME:-default}` fallback. |
| `no key (optional)` | Every referenced key is optional and absent; connected without one. |
| `missing key` | A required placeholder has no value; the server was not started. |
| `rejected` | The server answered `401` or `403` to a request that did carry a credential. |
| `credentials required` | The server answered `401` or `403`, the request carried none, and it did not ask for OAuth. |
| `oauth: not logged in` | The entry has an `oauth` block and nothing is stored yet. |
| `oauth: logged in` | A stored token was accepted, or is stored and not known to be expired. |
| `oauth: needs login` | The server asked for OAuth and no stored token or refresh worked. Run `/mcp login`. |
| `oauth: expired` | The stored token expired and the server issued no refresh token. |
| `oauth: refused` | The URL is plain `http` to a non-loopback host; no token was sent. |
| `oauth: client credentials` | A machine-to-machine grant; a token is fetched at every connect. |
| `oauth: rejected` | The authorization server or the MCP server refused the machine-to-machine credentials. |

## OAuth

An HTTPS server that asks for a bearer token, and has no `Authorization` header
configured, can be signed in to with `/mcp login <server>`:

1. Kerkov discovers the server's authorization server and shows its origin.
   That address comes from metadata the server controls, so check it before
   answering; declining cancels the login.
2. The browser opens the sign-in page. Kerkov listens on
   `http://127.0.0.1:<port>/callback` for the redirect; if the browser cannot
   reach it (a remote or headless host), paste the whole URL the browser ended
   on. A pasted URL must carry the `state` Kerkov sent; a code typed by hand on
   its own is accepted too.
3. The tokens, the client registration and the authorization server that issued
   them are stored in the credential store, bound to the server's URL, never in
   `mcp.json`.

The URL must be `https` unless the host is loopback, and so must every endpoint
of the authorization server — discovery, registration, sign-in and token —
whether just discovered or recorded with a stored login. OAuth is refused
(`oauth: refused`) rather than send a token, refresh token or authorization code
in the clear.

Later connects reuse the stored token and refresh it when it expires, against
the authorization server recorded with it. They never open a browser or register
a client: when a token stops working, the server shows `oauth: needs login` and
its tools fail with an error that says to run `/mcp login` again.
`/mcp logout <server>` forgets the stored login; it does not revoke it at the
server, and it says so rather than reporting success if the credential store
could not confirm the deletion.

Kerkov registers itself dynamically unless the entry names a pre-registered
`clientId`, which also needs the `callbackPort` it was registered with.

### Machine-to-machine

A server meant for unattended clients can take an OAuth client registered for
the `client_credentials` grant instead of a person's sign-in. Kerkov fetches a
token at every connect, with no browser and no `/mcp login`, and keeps it in
memory only: minting another needs nobody, so storing it would only leave a
bearer token at rest.

```json
"intel": {
  "type": "http",
  "url": "https://mcp.example.com/mcp",
  "oauth": {
    "grant": "client_credentials",
    "issuer": "https://auth.example.com",
    "clientId": "kerkov-ci",
    "clientSecret": "${INTEL_CLIENT_SECRET}",
    "scope": "mcp:read"
  }
}
```

| Key | Meaning |
|---|---|
| `grant` | `client_credentials` (a client secret) or `private_key_jwt` (a signed assertion, RFC 7523). Default `authorization_code`, the browser sign-in above. |
| `issuer` | Required. The authorization server the client is registered with. Credentials go only to the issuer the server's metadata names when it matches exactly; otherwise the connect fails before anything is sent. `https` unless on loopback. |
| `clientId` | Required. |
| `clientSecret` | `client_credentials` only, required. Write a `${NAME}` placeholder and store the value with `/mcp key <server> NAME`. |
| `tokenEndpointAuth` | `client_credentials` only: `client_secret_basic` (default) or `client_secret_post`. |
| `privateKey`, `privateKeyFile` | `private_key_jwt` only, exactly one: the PEM signing key as a placeholder, or the path of a PEM file. |
| `algorithm` | `private_key_jwt` only: `RS256` (default), `RS384`, `RS512`, `PS256`, `ES256`, `ES384` or `ES512`. |
| `scope` | The scope to request. A server that names a scope in its `401` challenge overrides it. |

The assertion Kerkov signs for `private_key_jwt` uses the client ID as issuer
and subject, the authorization server's issuer as audience, and lives five
minutes. The https rules above apply to every endpoint the grant reaches.
`/mcp login` does not apply to these servers; `/mcp connect` fetches a new
token.

## Tool names

`mcp__<server>__<tool>`, using the server's name from `mcp.json`. A tool name
with characters outside `A-Z a-z 0-9 _ -`, or one that would exceed 64
characters, is sanitized and given a short hash suffix; `/mcp list` shows each
mapped name beside the original. A tool whose input schema cannot be
represented (a recursive or non-local `$ref`, or an invalid schema) is skipped
with a warning, and the rest of the server's tools stay available.

## Resources

A server that offers [resources](https://modelcontextprotocol.io/specification/2026-07-28/server/resources)
— read-only context such as files, records or documents — gets one more tool,
`mcp__<server>__resources`, with an `action` of:

| Action | Effect |
|---|---|
| `list` | One line per resource: URI, name, MIME type and description. Pass the `cursor` it prints for the next page. |
| `templates` | The server's URI templates, such as `notes://hosts/{host}`, to fill in and read. |
| `read` | One resource's contents by `uri`. Text is returned inline; binary contents are saved to the run workspace like binary tool results. |

Kerkov checks at connect whether the server lists any resource or template, and
adds the tool only when it does: SDK-built servers advertise the capability even
with nothing to read, and every tool enlarges the cached prompt prefix. Set
`"resources": false` to withhold it anyway. A server tool that is itself named
`resources` keeps working under a hashed name. Reads are approved like the
server's tools: each one on its own unless the server is
[trusted](#trusted-servers), and a stored read is titled with its URI.

## Prompts

Servers can offer [prompts](https://modelcontextprotocol.io/specification/2026-07-28/server/prompts):
templates you pick, with arguments, that expand into a message. They are never
advertised to the model, so they do not touch the prompt cache.

- `/mcp prompts [server]` lists each connected server's prompts with their
  arguments; `*` marks a required one.
- `/mcp prompt <server> <prompt> [name=value ...]` fetches one and sends it as
  your next message. Quote a value with spaces: `topic="sql injection"`. The
  CLI asks for a required argument you left out; the web console reports it
  instead.

The rendered prompt is the user turn, exactly as if you had typed it. When a
prompt mixes roles, each part is labelled `[user]` or `[assistant]`; images and
binary resources in it are saved to the run workspace like tool results. Its
text comes from the server: read what it says before relying on it.

## Approval

Every MCP call prompts on its own, in every approval mode except `yolo`. Allow
rules, including hand-written ones, and `permissive` mode never approve an MCP
call; deny rules still refuse one without a prompt. A `--message` run, which
has nobody to ask, approves every call as it does for other tools.


- **No saved rules.** An MCP tool's arguments define no boundary Kerkov could
  safely widen to, and a server's schema may name `token` or `password`
  arguments that a saved rule would write verbatim into `approvals.yaml`. The
  prompt therefore offers no "always allow" option.
- **Annotations are ignored.** A server's `readOnlyHint` or `destructiveHint`
  is untrusted by the MCP specification and never changes approval.
- **Arguments are not logged.** Kerkov's log records argument names only, and
  a validation error names the failed constraint, not the submitted value.

### Trusted servers

`"trusted": true` on a server you control (or have vetted) makes its tools
behave like built-in tools for approval:

- allow rules, including hand-written ones, and `permissive` mode approve its
  calls without a prompt;
- the prompt offers "always allow": either any call of that tool, or this exact
  call. An argument whose name looks like a credential (`token`, `password`,
  `api_key`, `secret`, `authorization`, ...), or whose value is an object or
  list holding such a key at any depth, is left out of the saved rule, so its
  value never reaches `approvals.yaml`. The rest of the exact call is written
  as given;
- annotations are still ignored. Trust is your statement about the server, not
  the server's statement about its own tools.
- the same applies to its [resource tool](#resources), whose saved rule covers
  every read.

Trust is fixed when the server connects: after editing the flag, run
`/mcp connect <server>` (or `/mcp disconnect` then `/mcp connect` when it is
already connected). `/mcp list` and the web console's MCP panel mark trusted
servers.

## Agents

Delegated agents only receive tools their profile names, and no built-in
profile names an MCP tool. A user `AGENT.md` can opt in by listing the mapped
name, e.g. `mcp__burp__scan`, or every tool of a server with `mcp__burp__*`.

Agents approve whitelisted calls without asking anyone, so only a
[trusted](#trusted-servers) server's tools reach them: an untrusted server's
tools are withheld even when a profile names them, since each of their calls
must be approved by a person. A profile that names a trusted server's tool runs
it **without a prompt** at every spawn and depth, including spawns made through
nested `delegate`. See [Agent profiles](../agent-profiles.md#mcp-tools).

## Results

Text blocks are joined into the output. When a server sends only
`structuredContent`, that JSON is the output. Images, audio and binary
resources are written to the run workspace as
`mcp/<server>/<call-id>-<n>.<ext>` and the output names the path; base64 never
enters the conversation. A result marked `isError` is a failed call whose
message the model can read and correct.

A server that asks for input mid-call (elicitation, sampling or roots) is
declined, since Kerkov does not support those requests yet; the call fails with
an error naming what the server asked for. Stored artifacts are titled with the
call's scalar arguments, such as `mcp__intel__cve_lookup: cve_id=CVE-2024-3094`,
so `recall(source=...)` can find them by argument value.

## Trust

Server tool descriptions, schemas and results come from the server. Treat them
as untrusted data, especially when a server touches a target. Descriptions and
schemas sit in the cached system prompt for every request; filter large servers
with `tools.allow` to keep that prefix small.

## Commands

| Command | Effect |
|---|---|
| `/mcp`, `/mcp status` | One row per server: transport, state, auth, tools exposed of advertised. |
| `/mcp list [server]` | Mapped tool names beside the originals, the resource tool, and skipped tools and why. |
| `/mcp prompts [server]` | The prompt templates each connected server offers. |
| `/mcp prompt <server> <prompt> [name=value ...]` | Send one prompt as your next message. See [Prompts](#prompts). |
| `/mcp connect [server]` | Re-read `mcp.json`, close servers no longer in it, and connect one server, or every enabled one that is not connected. |
| `/mcp disconnect <server>` | Close one server and withdraw its tools. |
| `/mcp login <server>`, `/mcp logout <server>` | Sign in to an OAuth server in the browser, or forget its stored login. |
| `/mcp key <server> [NAME]` | Prompt for a referenced key, store it, and reconnect. `NAME` is needed only when the server references more than one. |
| `/mcp add <server> [-e KEY=VALUE ...] -- <command> [args...]` | Add a stdio server to the active `mcp.json` and connect it. Write `${KEY}` placeholders, not secrets. |
| `/mcp add <server> --url <url> [--transport http\|sse] [-H 'Name: value' ...]` | Add an HTTP server the same way; `--transport sse` for a legacy SSE endpoint. |
| `/mcp remove <server>` | Disconnect a server and delete its entry. |

The web console accepts `/mcp`, `/mcp status`, `/mcp list`, `/mcp prompts`,
`/mcp prompt`, `/mcp connect` and `/mcp disconnect`; the commands that enter
credentials or edit `mcp.json` stay in the CLI.

`connect`, `disconnect`, `add` and `remove` change the advertised tool list, so
the next request rebuilds the provider's prompt cache once. Nothing else
changes it: Kerkov lists a server's tools once per connection and ignores later
changes until you reconnect.

## Lifetime

Servers connect when the CLI starts and close on `/q`, Ctrl-C, end of input and
at the end of a `--message` run; an HTTP connection closes its client. `/clear`, `/mode` and `/resume` cancel calls
still running but keep connections, so a stateful server keeps its state across
engagements; `/mcp disconnect` and `/mcp connect` reset it. A stdio server's
stderr is written to `logs/mcp/<server>.log` rather than the terminal. See
[MCP client](../../concepts/mcp-client.md) for the mechanism.

The [web console](../../guides/web-console.md#mcp-servers) connects its own
servers for each browser connection and closes them when that connection
closes; clearing, changing mode and opening another engagement behave as in the
CLI. It uses stored keys and OAuth logins but cannot enter them, so `/mcp key`
and `/mcp login` stay in the CLI; `/mcp connect` in the console then picks up
what they stored.

## Related

- [Approvals](../../guides/approvals.md)
- [Agent profiles](../agent-profiles.md)
- [Configuration: MCP servers](../configuration.md#mcp-servers)
