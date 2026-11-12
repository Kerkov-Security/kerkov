# MCP servers

Kerkov is an [MCP](https://modelcontextprotocol.io) client. Every tool an MCP
server advertises becomes a Kerkov tool named `mcp__<server>__<tool>`, approved,
executed, indexed into the knowledge base and persisted like any built-in tool.

Two transports are supported:

- **stdio** — a local command that Kerkov starts as a child process and talks to
  over standard input and output;
- **streamable HTTP** — a remote or local server at a URL, authenticated with
  a key sent in a header or with an OAuth sign-in.

The legacy HTTP+SSE transport and machine-to-machine OAuth follow (tracked in
[#304](https://github.com/Kerkov-Security/kerkov/issues/304)).

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

1. `MCP_CONFIG_PATH`, when set;
2. `config/mcp.json` in a source checkout (git-ignored; copy
   `config/mcp.example.json`);
3. `~/.kerkov/mcp.json`.

!!! danger "Never read from the working directory"
    Kerkov does not load `.mcp.json` or `mcp.json` from the directory it runs in.
    Operators run Kerkov inside cloned target repositories, and a planted file
    would start an attacker's command.

| Key | Meaning |
|---|---|
| `type` | `stdio` or `http`. Inferred from `command` or `url` when omitted. `sse` entries are accepted and shown as not yet supported. |
| `url`, `headers` | The endpoint and extra request headers of an HTTP server. |
| `command`, `args` | The program to start and its arguments. |
| `env` | Extra environment for that server only. Kerkov's provider keys are never passed, even if listed. |
| `enabled` | `false` keeps the entry without connecting it. Default `true`. |
| `tools` | `{"allow": [...], "deny": [...]}` glob filters on the server's tool names, applied before advertising. Deny wins. |
| `optional` | Placeholder names that may be absent; see below. |
| `timeout` | Seconds to wait for one tool call. Default `300`. |
| `oauth` | HTTP only, all optional: `scope`, and `clientId` with `callbackPort` for a client registered in advance. See [OAuth](#oauth). |

Unknown keys produce a warning and are preserved when `/mcp add` or
`/mcp remove` rewrites the file. Server names are 1–32 letters, digits, `_` or
`-`, and may not contain `__`.

### Keys and placeholders

`${NAME}` and `${NAME:-default}` expand in `command`, `args`, `env`, `url` and
`headers`. Each name resolves from a session value, then the environment
(including `.env`), then Kerkov's credential store. Store one with
`/mcp key <server> [NAME]`, which prompts with masked input and reconnects the
server, or with `/auth set NAME`. The value never goes into `mcp.json`.

A placeholder is required unless it is listed in `optional`. A missing required
key keeps the server from starting. A missing optional key drops the header,
`env` entry or argument that uses it instead of sending an empty value, so a
server that also works anonymously gets no `Authorization: Bearer ` header. A
literal-looking secret in a key-like header or `env` entry produces a warning
suggesting a placeholder, and so does a header sent over plain `http://` to a
host other than loopback.

`/mcp` shows each server's auth state without ever showing a value:

| State | Meaning |
|---|---|
| `none` | No placeholders referenced. |
| `key (<source>)` | Keys resolved; the source is `environment`, `session` or the credential store. |
| `no key (optional)` | Every referenced key is optional and absent; connected without one. |
| `missing key` | A required placeholder has no value; the server was not started. |
| `rejected` | The server answered `401` or `403` to the key it was sent. |
| `credentials required` | The server answered `401` or `403`, no key is configured, and it did not ask for OAuth. |
| `oauth: not logged in` | The entry has an `oauth` block and nothing is stored yet. |
| `oauth: logged in` | A stored token was accepted, or is stored and not known to be expired. |
| `oauth: needs login` | The server asked for OAuth and no stored token or refresh worked. Run `/mcp login`. |
| `oauth: expired` | The stored token expired and the server issued no refresh token. |
| `oauth: refused` | The URL is plain `http` to a non-loopback host; no token was sent. |

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

The URL must be `https` unless the host is loopback: OAuth is refused rather
than send a bearer token in the clear.

Later connects reuse the stored token and refresh it when it expires, against
the authorization server recorded with it. They never open a browser or register
a client: when a token stops working, the server shows `oauth: needs login` and
its tools fail with an error that says to run `/mcp login` again.
`/mcp logout <server>` forgets the stored login; it does not revoke it at the
server, and it says so rather than reporting success if the credential store
could not confirm the deletion.

Kerkov registers itself dynamically unless the entry names a pre-registered
`clientId`, which also needs the `callbackPort` it was registered with.

## Tool names

`mcp__<server>__<tool>`, using the server's name from `mcp.json`. A tool name
with characters outside `A-Z a-z 0-9 _ -`, or one that would exceed 64
characters, is sanitized and given a short hash suffix; `/mcp list` shows each
mapped name beside the original. A tool whose input schema cannot be
represented (a recursive or non-local `$ref`, or an invalid schema) is skipped
with a warning, and the rest of the server's tools stay available.

## Approval

Every MCP call prompts on its own, in every approval mode except `yolo`:

- **No saved rules.** An MCP tool's arguments define no boundary Kerkov could
  safely widen to, and a server's schema may name `token` or `password`
  arguments that a saved rule would write verbatim into `approvals.yaml`. The
  prompt therefore offers no "always allow" option.
- **Annotations are ignored.** A server's `readOnlyHint` or `destructiveHint`
  is untrusted by the MCP specification and never changes approval.
- **Arguments are not logged.** Kerkov's log records argument names only.

## Agents

Delegated agents only receive tools their profile names, and no built-in
profile names an MCP tool. A user `AGENT.md` can opt in by listing the mapped
name, e.g. `mcp__burp__scan`. Agents auto-approve whitelisted calls, so such a
profile runs that tool **without a prompt** at every spawn and depth, including
spawns made through nested `delegate`.

## Results

Text blocks are joined into the output. When a server sends only
`structuredContent`, that JSON is the output. Images, audio and binary
resources are written to the run workspace as
`mcp/<server>/<call-id>-<n>.<ext>` and the output names the path; base64 never
enters the conversation. A result marked `isError` is a failed call whose
message the model can read and correct.

## Trust

Server tool descriptions, schemas and results come from the server. Treat them
as untrusted data, especially when a server touches a target. Descriptions and
schemas sit in the cached system prompt for every request; filter large servers
with `tools.allow` to keep that prefix small.

## Commands

| Command | Effect |
|---|---|
| `/mcp`, `/mcp status` | One row per server: transport, state, auth, tools exposed of advertised. |
| `/mcp list [server]` | Mapped tool names beside the originals, plus skipped tools and why. |
| `/mcp connect [server]` | Re-read `mcp.json` and connect one server, or every enabled one that is not connected. |
| `/mcp disconnect <server>` | Close one server and withdraw its tools. |
| `/mcp login <server>`, `/mcp logout <server>` | Sign in to an OAuth server in the browser, or forget its stored login. |
| `/mcp key <server> [NAME]` | Prompt for a referenced key, store it, and reconnect. `NAME` is needed only when the server references more than one. |
| `/mcp add <server> [-e KEY=VALUE ...] -- <command> [args...]` | Add a stdio server to the active `mcp.json` and connect it. Write `${KEY}` placeholders, not secrets. |
| `/mcp add <server> --url <url> [-H 'Name: value' ...]` | Add an HTTP server the same way. |
| `/mcp remove <server>` | Disconnect a server and delete its entry. |

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

The web console does not connect MCP servers yet; keys and OAuth logins stay
in the CLI.

## Related

- [Approvals](../../guides/approvals.md)
- [Agent profiles](../agent-profiles.md)
- [Configuration: MCP servers](../configuration.md#mcp-servers)
