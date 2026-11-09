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
| `tools/mcp/results.py` | `CallToolResult` to `ToolResult` |

The adapter lives under `tools/` so the approval-scope coverage test, which walks
`kerkov.tools`, checks it. The SDK is imported only inside the connection layer,
so the adapter module imports and is tested without the `mcp` extra.

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
from any task, so `call_tool` runs in the caller's task, raced against the
holder: if the server dies, the call fails at once instead of waiting for its
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
it inside the holder task. The SDK reports a `401` or `403` as a generic
JSON-RPC error, so a response hook on that client records the status; the
connection error and the `rejected` or `credentials required` auth state come
from it rather than from the SDK's message.

Resolved header values live only on the `ResolvedServer` (whose `repr` omits
them) and in that client; they are never logged or written back to `mcp.json`.

## OAuth

`core/mcp/oauth.py` plugs Kerkov into the SDK's `OAuthClientProvider`, which
does discovery, registration, PKCE, refresh and retry:

- `CredentialTokenStorage` is the SDK's `TokenStorage` over `CredentialManager`,
  with tokens and the client registration under separate keys (a keyring entry
  has a size limit), each stamped with the server URL so an edited URL never
  receives an old token.
- An ordinary connect attaches the provider only when a login is stored, and
  gives it handlers that raise instead of opening a browser. Without stored
  tokens there is no provider at all, so a connect can never register a client
  as a side effect. A `401` with a bearer challenge then reads as
  `oauth: needs login`.
- `/mcp login` passes a `LoopbackAuthorization`: the redirect handler shows the
  authorization server's origin and asks before opening the browser, and the
  callback handler reuses the Codex loopback listener and paste fallback. A
  login waits up to six minutes, since it waits on the operator.
- The SDK learns a token's lifetime only when it is issued. A small provider
  subclass restores the stored expiry on load; otherwise a token loaded after a
  restart would be sent until refused, and a refusal leads to a full login
  instead of a refresh.

## Cleanup paths

`_run_cli` closes every connection in a `finally`, which covers `/q` (it raises
`SystemExit`), Ctrl-C, end of input and one-shot `--message` runs. Engagement
switches (`/clear`, `/mode`, `/resume`) go through
`settle_agents_for_conversation_switch`, which cancels in-flight MCP calls but
leaves connections open: connections belong to the process, and reconnecting
would change the advertised tools and the prompt cache for no gain. A
client-side timeout does not prove the server stopped working, which the timeout
error says.

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

## Schemas

An MCP tool's `inputSchema` is arbitrary JSON Schema, not a Pydantic model. The
adapter backs `execute()` with a permissive params class
(`extra="allow"`), advertises `normalize_schema(inputSchema)` from
`tools/schema.py`, and validates arguments against the **original** schema with
`jsonschema` before the call is sent. A schema that normalization cannot inline,
or that is not a valid schema, makes that one tool unavailable.
