# Command-Line Interface

Kerkov installs two console commands: `kerkov` (the interactive assistant) and
`kerkov-web` (the web console). This page lists every flag on both.

## `kerkov`

```
kerkov [--mode {recon,audit,offensive}] [--continue | --resume <id>]
       [--message <text> ...] [--no-welcome]
       [--verbosity {guided,standard,aggressive,silent}]
       [--approval {strict,normal,permissive,yolo}] [--yolo]
       [--interface {cli,web}] [--version]
kerkov setup <component> [--timeout <seconds>]
```

With no arguments and an interactive terminal, `kerkov` first asks whether to
start the terminal CLI or the browser web console (and can remember the choice),
then starts an interactive session in the default **audit** mode. Provider,
model, mode, autonomy, and approvals can all be changed at runtime with
[slash commands](slash-commands.md).

### Options

| Flag | Argument | Default | Effect |
|---|---|---|---|
| `--mode` | `recon`, `audit`, `offensive` | `audit` | Operational mode at startup. `ctf` is not selectable here; switch to it at runtime with `/mode ctf`. Terminal CLI only: it starts the CLI over a saved `web` preference, and is refused with `--interface web` (pick the mode in the console instead). |
| `--continue` | — | — | Resume the most recent engagement. |
| `--resume` | `<engagement-id>` | — | Resume a specific engagement. If the id is an agent sub-run, it opens read-only. |
| `--message` | `<text>` | — | Run one message non-interactively, then exit. Repeatable; messages run in order. |
| `--no-welcome` | — | — | Suppress the welcome panel (for non-interactive or scripted runs). |
| `--verbosity` | `guided`, `standard`, `aggressive`, `silent` | preset | Pin narration verbosity, overriding `VERBOSITY` and the autonomy preset. |
| `--approval` | `strict`, `normal`, `permissive`, `yolo` | preset | Pin approval gating, overriding `APPROVAL_MODE` and the autonomy preset. |
| `--yolo` | — | — | Skip all tool-approval prompts for this run. Sugar for `--approval yolo`. |
| `--interface` | `cli`, `web` | saved / prompt | Start the terminal CLI or launch the web console. Overrides the saved `INTERFACE` preference; `web` requires the `web` extra. The web console honors `--approval`, `--verbosity`, and `--yolo` for that server run. |
| `--version` | — | — | Print the installed version and exit. |

There is deliberately no `--autonomy` flag: autonomy is a preset over the two
independent axes above. Set the starting level with `AUTONOMY_LEVEL`, or change
it at runtime with [`/autonomy`](slash-commands.md#modes-and-autonomy).

### Non-interactive use

`--message` runs one or more prompts and exits — the basis for scripted or CI
use. Pair it with `--no-welcome` for clean output:

```bash
kerkov --message "scan 10.0.0.1 for open services" --no-welcome
kerkov --message "enumerate" --message "then suggest next steps" --no-welcome
```

If stdin is not a TTY and no `--message` is given, Kerkov reads prompts from
stdin, one per non-empty line:

```bash
echo "summarize the findings ledger" | kerkov --no-welcome
```

### `kerkov setup`

Installs or verifies an optional local tool. Kerkov never installs these
silently; this is the explicit path.

| Argument | Values | Effect |
|---|---|---|
| `component` | `opengrep` | The optional tool to install or verify. |
| `--timeout` | seconds (default `180`) | Maximum time the setup step may take. |

```bash
kerkov setup opengrep
```

## `kerkov-web`

The web console is a single-operator, loopback-only UI over the same runtime. It
requires the web extra:

```bash
uv sync --extra web        # or: pip install "kerkov[web]"
kerkov-web runserver       # serves on http://127.0.0.1:8764
```

`uv run kerkov --interface web` starts the same server and opens a browser; the
startup prompt does the same when you pick **web**. `kerkov-web` is the direct
entry point and a thin wrapper over Django's management commands, so it accepts
the usual Django subcommands; `runserver` is the one you want. It loads the same
`.env` as the CLI.

| Behavior | Detail |
|---|---|
| Default bind | `127.0.0.1:8764` when no address is given. |
| Loopback only | Binding to a non-loopback host is refused — the console is unauthenticated by design. |
| Access token | Guarded by `KERKOV_WEB_ACCESS_TOKEN`; a fresh token is generated and printed at startup if unset. |

Pass an alternate loopback address the usual way:

```bash
kerkov-web runserver 127.0.0.1:9000
```

Configuration for both commands lives in [Configuration](configuration.md);
web-specific variables (`KERKOV_WEB_*`) are listed there under **Web console**.
