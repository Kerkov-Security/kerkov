# `exec` and `process`

`exec` runs commands. `process` drives the ones that are still running.

Together they are Kerkov's whole command-execution surface — there is no separate
shell tool. A command that prints and quits is one `exec` call; a command you
keep talking to (an SSH shell, a REPL, `msfconsole`) is one `exec` call that
returns a session id, and then a conversation of `process` calls against it.

The mechanism behind both — PTY allocation, terminal emulation, buffering,
process-tree termination — is documented in
[Process management](../../concepts/process-management.md).

## `exec`

### Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `command` | string | **required** | The command line to run. |
| `mode` | `one_shot` \| `session` \| `daemon` | **required** | What kind of work this is. See below. |
| `stdio` | `auto` \| `pty` \| `pipe` | `auto` | Terminal shape. `auto` gives every mode a PTY. `pipe` forces plain subprocess semantics; rejected with `mode=session`. |
| `workdir` | string | engagement workspace | Working directory for the command. |
| `timeout` | integer | `120` | Overall timeout in seconds, after which the process tree is killed. `one_shot` only; the other modes reject a non-default value. |
| `no_output_timeout` | integer | `60` | Kill the process after this many seconds with no output. `one_shot` only; `session` and `daemon` reject a non-default value. |

### Modes

`mode` is required because it is not inferable from the binary, and getting it
wrong is the difference between a useful session and a timeout.

| Mode | For | Returns | Timers |
|---|---|---|---|
| `one_shot` | Runs and exits on its own: `ls`, `curl`, `nmap`, a script that prints and quits. | The output, once it finishes. | Both `timeout` and `no_output_timeout` apply. |
| `session` | Anything you will send more input to: SSH and `su` shells, REPLs (`python3 -i`, `msfconsole`, `sqlite3`), and anything needing a terminal (`sudo` password prompts, `vim`, `less`). | A `session_id`, immediately. | Neither. Interactive sessions pause between commands by design. |
| `daemon` | Long-lived processes you start and leave: `openvpn`, tunnels, listeners. | A `session_id`, immediately. | Neither. A daemon going quiet after its handshake is normal. |

The rule of thumb: **if you will send a second command to the same target, you
want `session`, not a second `one_shot`.** Two `one_shot` calls are two
unrelated processes, so `cd /tmp` followed by `ls` lists your home directory.

### Approval

Prompts by default. `exec` is the one tool that offers **no broad rung at all**:
an approval here saves as that exact command or nothing.

This is deliberate. `argv[0]` is not a security boundary — `sudo *` and `bash *`
are blanket grants wearing a program name, and `curl *` exfiltrates. Widening
`exec` should be a considered edit to `approvals.yaml`, not a keystroke at a
prompt. `timeout` and `no_output_timeout` never reach a saved rule.

Three deny rules ship enabled, and a deny is refused without a prompt:

```yaml
- deny: "exec({'command': 're:.*rm.*-[rRf].*'})"
- deny: "exec({'command': 're:(^|[^a-z])sudo($|[^a-z])'})"
- deny: "exec({'command': 're:.*(mkfs|shred).*'})"
```

The `sudo` deny is the one operators most often relax; see
[Approvals](../../guides/approvals.md) for how to override packaged rules from
`~/.kerkov/approvals.yaml`. That deny rule is the only thing enforcing the sudo
boundary — `REQUIRE_SUDO_APPROVAL` is declared in the configuration but not read
by the runtime, so changing it has no effect.

Set [`EXECUTION_ENABLED=false`](../configuration.md#tool-execution) to refuse
every `exec` call outright; the result comes back with
`metadata.policy = "execution_disabled"`.

### Returns

For `one_shot`, the command's output, with `metadata.status` of `completed` or
`failed` alongside `exit_code`, `duration_ms`, `cwd`, `mode`, `stdio`, and — on a
kill — `exit_signal`, `timed_out` and `no_output_timed_out`, so a timeout is
distinguishable from a non-zero exit.

For `session` and `daemon`, a line naming the session id plus whatever the
process printed while starting. Metadata carries `status: "running"`,
`session_id`, `pid`, `started_at`, `cwd`, `mode`, and `stdio`.

`stdio` in the metadata is what the process **actually got**, which can differ
from what was asked: `auto` requests a PTY, and a platform that cannot supply one
falls back to pipes with a logged warning. Check it before assuming interactive
behavior works.

### Examples

A one-shot scan:

```json
{ "command": "nmap -sV -p- 10.10.10.5", "mode": "one_shot", "timeout": 900 }
```

An interactive session, then driving it:

```json
{ "command": "sshpass -p 'hunter2' ssh -o StrictHostKeyChecking=no bob@10.10.10.5", "mode": "session" }
```

```json
{ "action": "paste", "session_id": "4f2a9c1e", "text": "id; uname -a" }
```
```json
{ "action": "submit", "session_id": "4f2a9c1e" }
```
```json
{ "action": "poll", "session_id": "4f2a9c1e", "timeout": 3000 }
```

A daemon left running for the rest of the engagement:

```json
{ "command": "openvpn --config /home/kali/htb.ovpn", "mode": "daemon" }
```

Machine-readable output, no terminal:

```json
{ "command": "kubectl get pods -o json", "mode": "one_shot", "stdio": "pipe" }
```

`pipe` is worth reaching for when a PTY would corrupt the output: it keeps
stderr separate, reports `isatty()` as false, and suppresses the escape
sequences a program emits when it thinks it is on a terminal.

## `process`

Manages the sessions `exec` started. Every action but `list` needs a
`session_id`.

### Actions

| Action | What it does | Default approval |
|---|---|---|
| `list` | Every session, running and finished. | Allowed |
| `poll` | New output since the last read, optionally waiting for more. | Allowed |
| `log` | Full output history, paginated. | Allowed |
| `paste` | Send text into a PTY session. | Prompts |
| `submit` | Press Enter. | Prompts |
| `write` | Send raw bytes to stdin. Pipe sessions only. | Prompts |
| `send_keys` | Send control or special keys. | Prompts |
| `kill` | Terminate the session's process tree. | Prompts |

**Typing into a PTY session is `paste` then `submit`, not `write`.** `write`
exists for `stdio=pipe` sessions, where there is no terminal to paste into.
`send_keys` is only for keys that have no text form — `C-c`, `Up`, `Tab`,
`Escape`, `F1`–`F12` — never for typing.

### Parameters

| Parameter | Type | Default | Applies to |
|---|---|---|---|
| `action` | string | **required** | — |
| `session_id` | string | — | Every action except `list` |
| `text` | string | — | `paste` |
| `bracketed` | boolean | auto-detected | `paste`. Overrides the session's tracked bracketed-paste state. If a paste fails — a password prompt is the usual case — retry with `false`. |
| `data` | string | — | `write` |
| `eof` | boolean | `false` | `write`. Close stdin afterwards. |
| `keys` | array[string] | — | `send_keys`. Key tokens such as `["C-c"]`, `["Up"]`, `["S-Tab"]`. |
| `hex_bytes` | array[string] | — | `send_keys`. Raw bytes, e.g. `["0x04"]` for EOF. |
| `literal` | string | — | `send_keys`. A literal string, sent as-is. |
| `offset` | integer | — | `log`. Starting line. |
| `limit` | integer | last 200 lines | `log`. Line count. |
| `timeout` | integer | — | `poll`. Milliseconds to wait for new output, clamped to 120000. |

### Approval

An approval saved from a `process` prompt widens to **the action** — `kill` and
`log` are not the same grant. Four read-only actions ship auto-approved:

```yaml
- allow: "process({'action': 'list'})"
- allow: "process({'action': 'poll'})"
- allow: "process({'action': 'log'})"
```

`process` has no capability gate of its own. It drives sessions `exec` already
started, and making them unkillable by turning execution off mid-engagement
would be worse than the alternative.

### Returns

`list` returns a table of sessions with `running_count` and `finished_count` in
metadata. `poll` and `log` return output text with `status` and `session_id`;
`poll` waits in 250 ms slices up to its timeout and returns whatever arrived.
`kill` reports `status: "killed"`.

For a terminal-emulated session — which is what `mode=session` over a PTY gets —
`poll` returns newly rendered screen content and `log` returns the rendered
screen, rather than the raw byte stream with escape sequences stripped. The
distinction matters when a program redraws in place: `less` and `vim` produce a
screen, not a transcript.

### Examples

Interrupt a running command without killing the session:

```json
{ "action": "send_keys", "session_id": "4f2a9c1e", "keys": ["C-c"] }
```

Read the tail of a daemon's log:

```json
{ "action": "log", "session_id": "b70d3115", "limit": 50 }
```

Send a password to a prompt that rejected a bracketed paste:

```json
{ "action": "paste", "session_id": "4f2a9c1e", "text": "hunter2", "bracketed": false }
```

## Related

- [Process management](../../concepts/process-management.md) — PTY adapters,
  buffer caps, session TTL, termination and the key encoder.
- [`container_lifecycle`](container.md) — prefer it over `exec docker compose up`;
  it polls readiness locally instead of filling context.
- [`sbom_generate`](sbom-vulnerability-scan.md), [`sast_scan`](sast.md),
  [`zap`](zap.md) — prefer these over driving Syft, Grype, OpenGrep or ZAP
  through `exec`; they bound their output and keep full reports as artifacts.
- [Approvals](../../guides/approvals.md) — modes, rules, and the deny list.
