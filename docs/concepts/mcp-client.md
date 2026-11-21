# MCP client

How Kerkov holds MCP connections open, keeps the prompt cache stable, and keeps
server processes from outliving it. The operator-facing page is
[MCP servers](../reference/tools/mcp.md).

## Pieces

| Module | Role |
|---|---|
| `core/mcp/config.py` | `mcp.json` lookup, validation, placeholder resolution and round-tripping edits |
| `core/mcp/naming.py` | `mcp__<server>__<tool>` mapping, bounded to 64 characters |
| `core/mcp/connection.py` | One holder task per server |
| `core/mcp/manager.py` | Connects every server, builds tools, status rows, cancellation and shutdown |
| `tools/mcp/mcp_tool.py` | The `BaseTool` adapter |
| `tools/mcp/resource_tool.py` | The per-server resource tool |
| `tools/mcp/results.py` | Tool and resource results to `ToolResult` |

The adapter lives under `tools/` so the approval-scope coverage test, which walks
`kerkov.tools`, checks it. The SDK is imported only inside the connection layer,
so the adapter module imports and is tested without the `mcp` extra.

## Approval and trust

`McpTool` sets `BaseTool.prompts_every_call`, which both approval front ends
check after deny rules: allow rules and `permissive` mode skip the prompt for
other tools but never for this one. `ApprovalScope(saveable=False)` separately
keeps the prompt from offering a saved rule.

A server marked `trusted` gets `TrustedMcpTool` instances instead, chosen when
its tools are built at connect. Trust is a subclass rather than an instance
flag so that `approval_scope()` stays independent of instance state: the
approval-scope coverage test calls it on the class, and a subclass answers for
its own breadth there. `TrustedMcpTool` clears `prompts_every_call` and returns
`ApprovalScope.whole_tool()` with every credential-looking argument volatile —
named like one, or holding such a key anywhere in a nested value — since the
schema is the server's and no argument marks a boundary Kerkov could defend.

`load_config()` records the names a `.env` outside the Kerkov checkout added
to the environment (`ConfigManager.untrusted_env_names`). It drops
`MCP_CONFIG_PATH` among them, and `McpManager` resolves placeholders through an
environment view that hides the rest, so a target repository cannot choose a
server or fill its command line.

`load_dotenv` leaves those values in `os.environ` for the life of the process,
where a later load cannot tell them from the operator's own environment, and
`ProviderManager` and the web runtime both call `get_config(reload=True)`. The
provenance is therefore kept in `config_manager._DOTENV_INJECTED` until the
recorded value leaves the environment; stale records are dropped before
`load_dotenv` runs, so a trusted source may set the same name later.

`resolve_config_path` resolves a relative `MCP_CONFIG_PATH` through
`resolve_project_dir` — the Kerkov checkout, then `~/.kerkov` — because an
`MCP_CONFIG_PATH=./mcp.json` inherited from the shell would otherwise select the
inventory of whichever target repository Kerkov was started in.

## One holder task per connection

The SDK's `Client` exists only inside `async with`; it has no `connect()` or
`close()`. anyio also requires a cancel scope to be entered and exited in the
same task, so an `AsyncExitStack` cannot be kept on the manager and closed from
whichever task handles `/q`.

`McpConnection` therefore starts a holder task that enters the client, lists the
tools, reports them through a future, and then waits on a stop event. Closing
sets the event, so the holder leaves the `async with` in its own task and the
transport shuts down normally. The holder is cancelled only if that takes longer
than ten seconds.

Tool calls do not go through the holder. The entered client accepts requests
from any task, so `request` — which `call_tool` and the resource reads use —
runs in the caller's task, raced against the holder: if the server dies, the call fails at once instead of waiting for its
timeout. Cancelling the caller cancels the request.

## Process ownership

The SDK spawns a stdio server itself, in its own kill scope: a new process group
on POSIX, a Job Object on Windows. On shutdown it closes stdin, waits, and then
kills the whole tree, so `npx`-style shims do not leave children behind.
`core/process/` is not used: its adapters are PTY-based, and a PTY would corrupt
the JSON-RPC byte stream.

The child gets the SDK's minimal inherited environment (`PATH`, home and user
variables, and on Windows the system directories) plus that server's own `env`,
minus any Kerkov-managed credential. Its stderr goes to
`logs/mcp/<server>.log` so it never draws over the prompt.

## HTTP and credential failures

For an HTTP server Kerkov builds the `httpx2` client itself, with the server's
resolved headers and a 300-second read timeout for long tool calls, and enters
it inside the holder task. The legacy SSE transport uses the same client:
`sse_client` takes a client factory and enters what it returns, so Kerkov hands
it a factory that returns the prepared client, and the headers, OAuth provider,
status hook and URL redaction below apply to both transports unchanged. That
read timeout also bounds an idle SSE stream; SDK servers send keep-alive pings
well inside it. The SDK reports a `401` or `403` as a generic
JSON-RPC error, so a response hook on that client records the status; the
connection error comes from it rather than from the SDK's message.

Which of the two credential states that status becomes is decided by what the
request carried, not by which placeholders resolved: a literal header, a
`${NAME:-default}` fallback and a key in the query all read as `rejected`,
because a key the server refused and a key that was never configured look the
same on the wire. `ResolvedServer.sends_credentials` holds that answer, taken
from the resolved headers and URL.

Resolved header values live only on the `ResolvedServer` (whose `repr` omits
them) and in that client; they are never logged or written back to `mcp.json`.
A `url` is different: `httpx2` logs every request's full URL at INFO, and
Kerkov's root logger writes that to the session log. A placeholder may sit
anywhere in the URL, so `redact_urls` answers both halves of that. Structurally,
a logged URL keeps only its scheme, host and port: the userinfo, path and query
all become `/<redacted>`, which covers a key written into the file as a literal
as well as a substituted one. By provenance, each value `resolve_server`
substituted into the `url` is put back as its `${NAME}`, which is what covers a
key in the host, where there is no path to cut. That is the value actually sent,
so a `${NAME:-default}` fallback is named too, and it is matched without regard
to case and in its IDNA form, because `httpx2` logs the normalized host.
`ResolvedServer.url_substitutions` carries those pairs; a connection registers
them while its HTTP client is open, because a log record names no server. The
registry is keyed by connection, not server name: each web socket has its own
manager, so two tabs can hold the same server, and closing one must not drop
the other's substitutions.

The same redaction runs over the text of a failed request before it becomes
`ConnectionState.FAILED`'s error, since that reaches the session log, the `/mcp`
status row and the model. The filter itself goes on the `httpx2` logger —
`httpx2` is the SDK's client and nothing else in Kerkov uses it.

`mcp.json` is parsed before any value is known, so the endpoint checks split in
two. Syntax belongs to parsing: a malformed `url` is that one entry's
`McpConfigError`, which keeps a bad bracketed host from aborting the whole
inventory. Anything that depends on the substituted value — the plain-HTTP
header warning — is computed in `resolve_server`, carried on
`ResolvedServer.warnings`, and handed to the manager's `on_warning` hook before
the connection is built, including on an explicit `/mcp connect`. Printing it
from the status row afterwards would arrive after the header was already sent.

## OAuth

`core/mcp/oauth.py` plugs Kerkov into the SDK's `OAuthClientProvider`, which
does discovery, registration, PKCE, refresh and retry:

- `CredentialTokenStorage` is the SDK's `TokenStorage` over `CredentialManager`,
  with the tokens, the client registration and the authorization server's
  metadata under separate keys (a keyring entry has a size limit), each stamped
  with the server URL so an edited URL never receives an old token.
  `forget_login` deletes all three by server name — a `${KEY}` the URL can no
  longer resolve must not be the reason a usable token stays stored — and
  returns what the credential store could not confirm as removed, which
  `/mcp logout` prints instead of claiming success.
- OAuth is refused outright for a plain-HTTP URL on a non-loopback host, checked
  on the *resolved* URL at connect and in `/mcp login`. The parse-time warning
  only sees configured headers, so it cannot see the `Authorization` header the
  provider adds. The same rule covers the authorization server: the provider
  subclass checks every request the SDK's auth flow yields, before it is sent,
  and the redirect handler checks the authorization URL. Stored metadata is as
  server-controlled as discovered metadata, and a restored token endpoint
  receives a refresh POST before any discovery, so a check on the resource URL
  alone would let a refresh token go out in the clear.
- An ordinary connect attaches the provider only when a login is stored, and
  gives it handlers that raise instead of opening a browser. Without stored
  tokens there is no provider at all. The SDK registers a client *before* it
  calls the redirect handler, so refusing there would be too late: the provider
  subclass watches the flow's own requests and refuses the registration POST
  itself unless an unfinished `/mcp login` is driving it. A `401` with a bearer
  challenge reads as `oauth: needs login`.
- `/mcp login` passes a `LoopbackAuthorization`: the redirect handler shows the
  authorization server's origin and asks before opening the browser, and the
  callback handler reuses the Codex loopback listener and paste fallback. Both
  forward the RFC 9207 `iss` the authorization server sent, which the SDK checks
  against the issuer it discovered — and whose absence it rejects when the
  server advertised the parameter. A pasted callback *URL* must carry the
  expected `state`; only a bare code typed by hand is exempt, since the operator
  read it off the authorization server's own page. A login waits up to six
  minutes, since it waits on the operator.
- The SDK learns a token's lifetime only when it is issued. A small provider
  subclass restores the stored expiry on load; otherwise a token loaded after a
  restart would be sent until refused, and a refusal leads to a full login
  instead of a refresh. It restores the expiry only together with the stored
  authorization server metadata, validated against the issuer the stored
  registration is bound to: with no metadata the SDK's token endpoint falls back
  to the *resource* server's own origin, so refreshing there would hand the
  refresh token to whoever answers it. A login stored without that record is
  left to a `401` and a fresh `/mcp login`.
- The plaintext and registration guard is a mixin, `_GuardedFlow`, placed ahead
  of whichever SDK provider class is in use, so the machine-to-machine grants
  (`ClientCredentialsOAuthProvider`, `PrivateKeyJWTOAuthProvider`) get the same
  per-request check as the browser flow. They always pass the configured
  `issuer` to the SDK, which then builds a token request only from metadata for
  exactly that issuer: without it the server being connected to would choose
  where the client secret goes. Their tokens live in a per-connection memory
  store, not `CredentialTokenStorage`; every connect mints a new one.

## Cleanup paths

`_run_cli` closes every connection in a `finally`, which covers `/q` (it raises
`SystemExit`), Ctrl-C, end of input and one-shot `--message` runs. Engagement
switches (`/clear`, `/mode`, `/resume`) go through
`settle_agents_for_conversation_switch`, which cancels in-flight MCP calls but
leaves connections open: connections belong to the process, and reconnecting
would change the advertised tools and the prompt cache for no gain. A
client-side timeout does not prove the server stopped working, which the timeout
error says.

The web console builds one `McpManager` per WebSocket connection, beside that
connection's browser tool, and connects it in `WebKerkovSession.initialize`
concurrently with provider validation, before `session.ready` lists the tools.
`_retire_engagement` (`/clear`, `/mode`, Engagement-tab new and resume) cancels
in-flight calls, before agents are settled; disconnect cancels them in `close()`
and closes every connection in `aclose()`. A disconnect before `session.ready`
cannot reach the consumer's `disconnect()`, which waits for `connect()` to
return; the ASGI server cancels the application instead, so a cancelled
`initialize` runs `aclose()` itself before re-raising. Registration replaces the MCP tools in
`_master_tools_snapshot` in place, because each engagement's delegate tool is
rebuilt from that list, and puts them in `tools` ahead of `delegate` and
`agent_wait`, the order `_configure_agents` uses, so a later engagement on the
same connection advertises the same prefix.

`/mcp connect` in both front ends goes through `McpManager.reconnect`, which
re-reads the file, closes removed servers and connects the rest concurrently.
The console runs `/mcp` inside the message lock, so the tool list never changes
under a running turn, and answers with one `mcp.result` event that also
carries the new status for the Settings panel.

## Prompt-cache stability

Tool schemas sit inside Anthropic's cached prelude, which must stay
byte-identical across turns. So:

- tools are listed once per connection and frozen; Kerkov does not subscribe to
  `toolsListChanged`;
- `McpManager.tools()` sorts by mapped name rather than trusting server order;
- each adapter normalizes its schema once, at construction;
- only `/mcp connect`, `disconnect`, `add` and `remove` change the list, and
  they say so.

`tests/tools/mcp/mcp_tool_test.py` checks that two builds of an Anthropic request
with MCP tools are byte-identical.

## Resources

The front ends swap MCP tools as a set by `isinstance(tool, McpServerTool)`, the
common base of the per-tool adapter and the per-server resource tool. Resource
tools come from the same connect: the holder records the server's capabilities
from the initialize result, and the manager then lists resources and templates
once. Only a non-empty answer adds `mcp__<server>__resources`, because SDK
servers advertise the capability unconditionally and the tool would otherwise
sit in every cached prefix. That name is added to the taken set before the
server's own tools are mapped, whether or not resources are offered, so a tool
named `resources` always maps to the same hashed name. Like tools, the resource
tool is a plain or a trusted class chosen at connect, so an agent profile's
`mcp__<server>__*` grant covers it only for a trusted server.

## Schemas

An MCP tool's `inputSchema` is arbitrary JSON Schema, not a Pydantic model. The
adapter backs `execute()` with a permissive params class
(`extra="allow"`), advertises `normalize_schema(inputSchema)` from
`tools/schema.py`, and validates arguments against the **original** schema with
`jsonschema` before the call is sent. A schema that normalization cannot inline,
or that is not a valid schema, makes that one tool unavailable.
