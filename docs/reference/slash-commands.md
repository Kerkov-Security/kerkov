# Slash Commands

Anything you type that begins with `/` is a command; everything else is a
message to the model. Commands run locally and never reach the provider. Below
is every command, grouped as `/help` groups them, with its aliases and syntax.

A mistyped command such as `/aproval` is rejected with an error that suggests
the closest match; it is not sent to the model. The web console likewise
refuses commands it does not handle and names the ones that only work in the
terminal. Text whose first word is a path (`/etc/hosts has a stale entry`) or a
bare `/` is still an ordinary message.

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
| `/mode` | `/mode [red\|purple\|blue]` | Show or switch the operational mode (security-team role). Each carries its own system prompt and posture: red (offensive), purple (default), blue (defensive). The old names (`recon`, `audit`, `offensive`, `ctf`) are no longer accepted. |
| `/autonomy` | `/autonomy [<1-4\|guided\|standard\|aggressive\|autonomous>]` | Show or set the preset that adjusts verbosity **and** approval together. |
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
| `/usage` | — | `/usage [<engagement-id>]` | Show saved token usage and optional estimated costs for the current or named engagement, including delegated agents. Costs require `MODEL_PRICING_FILE`; missing rates remain unknown. |
| `/asset` | `/assets` | `/asset [list] \| /asset add <kind> <value> ["name"] \| /asset remove <id>` | List, add, or remove the resources being defended in the current engagement. |
| `/target` | `/targets` | `/target [list] \| /target add <kind> <value> ["name"] \| /target remove <id>` | List, add, or remove the subjects of offensive assessment in the current engagement. |

`/asset` and `/target` write the [findings ledger](tools/ledger.md)'s resource
sections directly, without going through the model. `kind` is one of `domain`, `ip`,
`url` (HTTP/S), or `repository` (an absolute local path, or `~/path`); quote values
or names containing spaces. Records belong to the current engagement, survive
`/resume`, and are shared with its agents. Adding one before the first chat message
starts an engagement. Both require `KB_ENABLED=true`, and neither role changes tool
approvals.

## Workflows

| Command | Aliases | Syntax | What it does |
|---|---|---|---|
| `/workflow` | `/workflows` | `/workflow [list] \| /workflow <name> runs \| /workflow <name> show <run-id> \| /workflow compliance assess\|preview \| /workflow compliance apply\|reassess <run-id>` | List the guided workflows and the team role each targets, run one, or review its saved runs. |

`assess` and `preview` prompt for a recorded `ip` or `domain` asset, a baseline
(or an existing profile project) and SSH settings, prefilled from SSH access
recorded in the ledger. `apply` re-issues a completed preview's exact plan, and
`reassess` repeats a run's assessment with its original asset and settings, so
neither asks again. During a run, tool calls outside the plan are refused and
the rest go through your approval mode; Esc or Ctrl-C stops it. Running a
workflow in a mode other than its team role only warns, because `/mode` starts a
new engagement. Runs need the interactive CLI: `--message` auto-approves tool
calls and cannot answer the setup prompts.

Runs are stored in the engagement workspace, so the terminal and the
[web console](../guides/web-console.md) read the same history. `show` prints the
stage progress, readiness checks, a per-control before/after table, and evidence
paths. A run ID may be shortened to a unique prefix of at least 6 characters.

## Tools

| Command | Syntax | What it does |
|---|---|---|
| `/zap` | `/zap [status\|stop]` | Inspect or stop Kerkov's managed ZAP scanner. |
| `/searxng` | `/searxng [setup\|start\|stop\|status]` | Install and run a local SearXNG instance for `web_search`. |
| `/mcp` | `/mcp [status\|list [server]\|prompts [server]\|prompt <server> <prompt> [name=value ...]\|connect [server]\|disconnect <server>\|key <server> [NAME]\|login <server>\|logout <server>\|add <server> ...\|remove <server>]` | Show, connect, add or remove [MCP servers](tools/mcp.md), or send one of their prompts. |

## Auth

| Command | Aliases | Syntax | What it does |
|---|---|---|---|
| `/auth` | `/credentials` | `/auth [set <target> [--session] \| remove <target>]` | Show, enter, or remove API credentials. `<target>` is a provider, tool, or profile env var; `--session` keeps a secret in memory only. |
| `/logout` | `/signout` | `/logout [<target>]` | Remove a stored credential through a confirmed picker. |

!!! tip "See it in the CLI"
    `/help` lists all of the above; `/help <command>` prints one command's full
    syntax, aliases, examples, and related commands.
