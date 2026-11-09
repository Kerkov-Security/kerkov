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

## Approval and trust

`McpTool` sets `BaseTool.prompts_every_call`, which both approval front ends
check after deny rules: allow rules and `permissive` mode skip the prompt for
other tools but never for this one. `ApprovalScope(saveable=False)` separately
keeps the prompt from offering a saved rule.

`load_config()` records the names a `.env` outside the Kerkov checkout added
to the environment (`ConfigManager.untrusted_env_names`). It drops
`MCP_CONFIG_PATH` among them, and `McpManager` resolves placeholders through an
environment view that hides the rest, so a target repository cannot choose a
server or fill its command line.

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
