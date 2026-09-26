# Your First Engagement

This walkthrough takes about ten minutes and assumes Kerkov is
[installed](install.md) and you have a provider credential ready. The target is
your own machine — the safest place to start.

!!! danger "Authorized use only"
    Everything below runs against `127.0.0.1`. Point Kerkov at another host only
    when you own it or have explicit written permission, and keep every action
    inside the agreed scope.

!!! note "Illustrative output"
    The transcripts on this page are representative of a real run, not captured
    from one. Exact tool output, timings, and phrasing vary by target, model,
    and version.

## 1. Launch

```bash
uv run kerkov
```

Kerkov starts in **audit** mode. On first run it asks you to choose a provider
and paste an API key at a masked prompt; the key is validated and saved to your
keyring, and you land at the `kerkov ›` prompt.

## 2. Describe the target and the objective

Kerkov is conversational — you state scope and intent in plain language, not
flags. Be explicit about the boundary:

```text
kerkov › Enumerate the services listening on this machine (127.0.0.1 only).
         Tell me what's exposed and flag anything that looks risky.
```

The assistant narrates what it intends to do, then makes its first tool call.
Because you're in the default posture, a call that isn't pre-approved pauses for
your confirmation:

```text
● I'll start with a quick TCP scan of localhost to see what's listening.

  exec  nmap -sT -Pn 127.0.0.1
  Approve this command? [y/N]
```

Type `y` to run it. Read-only tools like file reads run without asking; see
[Approvals](../guides/approvals.md) to change what prompts.

## 3. Let it work

Kerkov runs the scan, reads the output, and reasons about it — often chaining a
few tool calls, each gated by your approval policy:

```text
● Three services are up:
    22/tcp   ssh      OpenSSH 9.6p1
    80/tcp   http     nginx 1.26.0
    5432/tcp postgresql  PostgreSQL 16.2

  Port 5432 is bound to all interfaces. Let me confirm it's reachable and
  whether it accepts unauthenticated connections.

  exec  ss -tlnp | grep 5432
  Approve this command? [y/N]
```

You stay in the loop the whole way: approve, deny, or type a new instruction to
redirect it.

## 4. Record what matters

Confirmed facts go into the **findings ledger**, which stays in context even as
the conversation is compacted. You can ask for it directly:

```text
kerkov › Add the exposed Postgres port to the ledger and summarize what we know.
```

```text
● Recorded. Current findings:
    hosts     127.0.0.1
    services  22/ssh, 80/http (nginx 1.26.0), 5432/postgresql (16.2)
    findings  PostgreSQL 16.2 listening on 0.0.0.0:5432 — review exposure
```

See [Memory: recall and the ledger](../guides/memory.md) for what persists and
how to query bulky tool output later.

## 5. Useful launch patterns

You'll often start Kerkov in a particular posture or resume earlier work:

```bash
# Passive discovery only — no exploitation
uv run kerkov --mode recon

# Resume the most recent engagement
uv run kerkov --continue

# Resume a specific engagement
uv run kerkov --resume <engagement-id>
```

## 6. Leave and come back

Exit with `/q`. Kerkov prints the command to resume this exact engagement.
Everything — history, findings, and captured tool output — is saved locally
under `~/.kerkov/engagements/`.

## Where to go next

- [Modes and autonomy](../guides/modes-and-autonomy.md) — match posture and
  hands-off level to the task.
- [Approvals](../guides/approvals.md) — make safe, repetitive work frictionless.
- [Engagements](../guides/engagements.md) — resume, list, and manage saved work.
- [Slash commands](../reference/slash-commands.md) — everything you can type.
