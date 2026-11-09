# MCP servers

Kerkov is an [MCP](https://modelcontextprotocol.io) client. Every tool an MCP
server advertises becomes a Kerkov tool named `mcp__<server>__<tool>`, approved,
executed, indexed into the knowledge base and persisted like any built-in tool.

This release connects **stdio** servers: local commands that Kerkov starts as a
child process and talks to over standard input and output. Remote servers over
streamable HTTP, static header keys and OAuth follow (tracked in
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

    For the same reason, a `.env` Kerkov finds in the working directory or its
    parents, other than the one at the root of the Kerkov checkout, cannot set
    `MCP_CONFIG_PATH` (it is ignored with a warning) and cannot fill MCP
    placeholders. Export those in the shell or store them with `/auth set`.

| Key | Meaning |
|---|---|
| `type` | `stdio`. Inferred from `command` when omitted. `http` entries are accepted and shown as not yet supported. |
| `command`, `args` | The program to start and its arguments. |
| `env` | Extra environment for that server only. Kerkov's provider keys are never passed, even if listed. |
| `enabled` | `false` keeps the entry without connecting it. Default `true`. |
| `tools` | `{"allow": [...], "deny": [...]}` glob filters on the server's tool names, applied before advertising. Deny wins. |
| `optional` | Placeholder names that may be absent; see below. |
| `timeout` | Seconds to wait for one tool call. Default `300`. |

Unknown keys produce a warning and are preserved when `/mcp add` or
`/mcp remove` rewrites the file. Server names are 1–32 letters, digits, `_` or
`-`, and may not contain `__`.

### Keys and placeholders

`${NAME}` and `${NAME:-default}` expand in `command`, `args` and `env`. Each name
resolves from a session value, then the environment (including Kerkov's own
`.env`, but not one from a target's directory), then Kerkov's credential store. Store one with `/auth set NAME`; the value never goes
into `mcp.json`.

A placeholder is required unless it is listed in `optional`. A missing required
key keeps the server from starting and shows `missing key` in `/mcp`. A missing
optional key drops the `env` entry or argument that uses it instead of passing
an empty value. A literal-looking secret in a key-like `env` entry produces a
warning suggesting a placeholder.

## Tool names

`mcp__<server>__<tool>`, using the server's name from `mcp.json`. A tool name
with characters outside `A-Z a-z 0-9 _ -`, or one that would exceed 64
characters, is sanitized and given a short hash suffix; `/mcp list` shows each
mapped name beside the original. A tool whose input schema cannot be
represented (a recursive or non-local `$ref`, or an invalid schema) is skipped
with a warning, and the rest of the server's tools stay available.

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
| `/mcp connect [server]` | Re-read `mcp.json`, close servers no longer in it, and connect one server, or every enabled one that is not connected. |
| `/mcp disconnect <server>` | Close one server and withdraw its tools. |
| `/mcp add <server> [-e KEY=VALUE ...] -- <command> [args...]` | Add a stdio server to the active `mcp.json` and connect it. Quote `${KEY}` placeholders, not secrets. |
| `/mcp remove <server>` | Disconnect a server and delete its entry. |

`connect`, `disconnect`, `add` and `remove` change the advertised tool list, so
the next request rebuilds the provider's prompt cache once. Nothing else
changes it: Kerkov lists a server's tools once per connection and ignores later
changes until you reconnect.

## Lifetime

Servers connect when the CLI starts and close on `/q`, Ctrl-C, end of input and
at the end of a `--message` run. `/clear`, `/mode` and `/resume` cancel calls
still running but keep connections, so a stateful server keeps its state across
engagements; `/mcp disconnect` and `/mcp connect` reset it. A stdio server's
stderr is written to `logs/mcp/<server>.log` rather than the terminal. See
[MCP client](../../concepts/mcp-client.md) for the mechanism.

The web console does not connect MCP servers yet.

## Related

- [Approvals](../../guides/approvals.md)
- [Agent profiles](../agent-profiles.md)
- [Configuration: MCP servers](../configuration.md#mcp-servers)
