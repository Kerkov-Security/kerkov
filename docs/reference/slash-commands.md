# Slash Commands

Anything you type that begins with `/` is a command; everything else is a
message to the model. Commands run locally and never reach the provider. Below
is every command, grouped as `/help` groups them, with its aliases and syntax.

`<angle brackets>` mark required arguments, `[square brackets]` optional ones,
and `a|b` a choice. With no argument, the info-style commands (`/mode`,
`/provider`, `/model`, `/autonomy`, `/verbosity`, `/approval`, `/auth`, `/zap`,
`/searxng`) show current state instead of changing it.

## Session

| Command | Aliases | Syntax | What it does |
|---|---|---|---|
| `/help` | — | `/help [<command>]` | List every command by category; pass a command for its full docs. |
| `/prompt` | — | `/prompt [<path>]` | Compose a message in `$EDITOR`, or send one loaded from a file. |
| `/clear` | — | `/clear` | Clear history, token tracker, and session approval rules. Mode, autonomy, provider, and model are kept. |
| `/q` | `quit`, `exit` | `/q` | Exit Kerkov and print the command to resume this engagement. |

## Modes and autonomy

| Command | Syntax | What it does |
|---|---|---|
| `/mode` | `/mode [recon\|audit\|offensive\|ctf]` | Show or switch the operational mode. Each carries its own system prompt and posture. `ctf` is runtime-only (no `--mode ctf`). |
| `/autonomy` | `/autonomy [<1-4\|guided\|standard\|aggressive\|autonomous>]` | Show or set the preset that adjusts verbosity **and** approval together. Level 4 requires `ALLOW_AUTONOMOUS=true`. |
| `/verbosity` | `/verbosity [guided\|standard\|aggressive\|silent]` | Change narration only, independent of approvals. |
| `/approval` | `/approval [strict\|normal\|permissive\|yolo]` | Change approval gating only, independent of narration. |
| `/yolo` | `/yolo [on\|off]` | Toggle the approval bypass; sugar for `/approval yolo\|normal`. |

## Providers and models

| Command | Aliases | Syntax | What it does |
|---|---|---|---|
| `/provider` | `/providers` | `/provider [<name>]` | Open the provider picker, or switch directly by name. Selecting Codex starts browser sign-in when needed. |
| `/model` | `/models` | `/model [<name>]` | Open the model picker for the current provider, or switch directly by name. |

## Skills

| Command | Syntax | What it does |
|---|---|---|
| `/skill` | `/skill [search <query> \| <name> [args]]` | List skills, search them, or load one with optional input. |

## Agents

| Command | Syntax | What it does |
|---|---|---|
| `/agents` | `/agents [<name\|id>] \| /agents stop <name\|id> \| /agents review <name\|id> [on\|off]` | List this session's agents and switch which one you're watching; stop one; or toggle operator review of its tool calls (only while attached). |
| `/back` | `/back` | Return focus to the main conversation. |

`Ctrl-O` is a keybinding for `/agents`. Any text already typed is stashed and
restored as the next prompt's default. It works both at the chat prompt and
mid-turn (POSIX TTY only).

## Engagements

| Command | Aliases | Syntax | What it does |
|---|---|---|---|
| `/engagements` | — | `/engagements [agents]` | List saved engagements, or the agent sub-runs behind them. |
| `/resume` | — | `/resume [<id>]` | Resume a previous engagement; opening an agent sub-run's id shows it read-only. |
| `/asset` | `/assets` | `/asset [list] \| /asset add <kind> <value> ["name"] \| /asset remove <id>` | List, add, or remove the resources being defended in the current engagement. |
| `/target` | `/targets` | `/target [list] \| /target add <kind> <value> ["name"] \| /target remove <id>` | List, add, or remove the subjects of offensive assessment in the current engagement. |

`/asset` and `/target` write the [findings ledger](tools/ledger.md)'s resource
sections directly, without going through the model. `kind` is one of `domain`, `ip`,
`url` (HTTP/S), or `repository` (an absolute local path, or `~/path`); quote values
or names containing spaces. Records belong to the current engagement, survive
`/resume`, and are shared with its agents. Adding one before the first chat message
starts an engagement. Both require `KB_ENABLED=true`, and neither role changes tool
approvals.

## Tools

| Command | Syntax | What it does |
|---|---|---|
| `/zap` | `/zap [status\|stop]` | Inspect or stop Kerkov's managed ZAP scanner. |
| `/searxng` | `/searxng [setup\|start\|stop\|status]` | Install and run a local SearXNG instance for `web_search`. |

## Auth

| Command | Aliases | Syntax | What it does |
|---|---|---|---|
| `/auth` | `/credentials` | `/auth [set <target> [--session] \| remove <target>]` | Show, enter, or remove API credentials. `<target>` is a provider, tool, or profile env var; `--session` keeps a secret in memory only. |
| `/logout` | `/signout` | `/logout [<target>]` | Remove a stored credential through a confirmed picker. |

!!! tip "See it in the CLI"
    `/help` lists all of the above; `/help <command>` prints one command's full
    syntax, aliases, examples, and related commands.
