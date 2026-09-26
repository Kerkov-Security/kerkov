# Non-Interactive Use

Kerkov can run without its interactive UI — driven by flags or piped input — so
it fits into scripts, CI jobs, and one-off automation.

## Running one or more messages

`--message` processes a prompt and exits. Pass it more than once to run a
sequence in order; pair it with `--no-welcome` for clean output:

```bash
kerkov --message "Summarize the security boundaries in this repository" --no-welcome

kerkov --message "Enumerate services on 127.0.0.1" \
       --message "Then recommend next steps" \
       --no-welcome
```

## Piping from stdin

If stdin is not a terminal and you give no `--message`, Kerkov reads prompts
from stdin, one per non-empty line:

```bash
echo "Show the findings ledger" | kerkov --no-welcome

kerkov --no-welcome <<'EOF'
Enumerate the listening services on 127.0.0.1
Flag anything exposed to non-loopback interfaces
EOF
```

## Approvals in non-interactive runs

!!! danger "Non-interactive runs auto-approve tool calls"
    There is no operator to answer a prompt, so tool calls that would otherwise
    prompt are auto-approved. Deny rules from your
    [approval configuration](approvals.md) still apply, but treat a
    non-interactive run as effectively hands-off.

    Run these only in an isolated environment, with a tightly scoped prompt and
    target. For extra clarity you can make it explicit with `--approval` or
    pre-flight your [approval rules](approvals.md).

## Combining with other flags

Non-interactive runs take the same flags as an interactive session — set the
mode and posture up front:

```bash
kerkov --mode recon \
       --message "Map the attack surface of 10.0.0.5 (authorized)" \
       --approval permissive \
       --no-welcome
```

See the [CLI reference](../reference/cli.md) for every flag, and
[Engagements](engagements.md) — non-interactive runs are saved like any other
and can be resumed interactively with `--continue` or `--resume`.

## Scripted use in CI

Because a run exits when its messages are done, a security check fits in a CI
step:

```bash
kerkov --message "Run a SAST scan on ./src and report high-severity findings" \
       --no-welcome
```

Set provider credentials through environment variables (see the
[configuration reference](../reference/configuration.md#provider-credentials-and-endpoints))
rather than the interactive prompt, which isn't available in CI.
