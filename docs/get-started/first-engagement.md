# Your First Engagement

This walkthrough takes about ten minutes and assumes Kerkov is
[installed](install.md) and you have a provider credential ready. The target is
your own machine — the safest place to start.

!!! danger "Authorized use only"
    Everything below runs against `127.0.0.1`. Point Kerkov at another host only
    when you own it or have explicit written permission, and keep every action
    inside the agreed scope.

The transcript on this page is captured from a real run. Exact tool output,
timings, and phrasing vary by target, model, and version — this run used the
`codex` provider, which fell back to `gpt-5.6-sol`.

## 1. Launch

```bash
kerkov
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

The assistant narrates what it intends to do, then makes its first tool call. In
the default posture, a call that isn't pre-approved pauses for your confirmation
(`Approve this command? [y/N]`); read-only tools run without asking. See
[Approvals](../guides/approvals.md) to change what prompts. The run below was
captured with approvals auto-accepted, so those prompts don't appear in the
output.

## 3. Let it work

Kerkov runs the scan, reads the output, and reasons about it — chaining several
tool calls. The output below is real, captured from the run; the only redaction
is the VS Code Server connection token, shown as `[redacted]`.

```text
✓ Connected to gpt-5.6-sol
Workspace: /home/kali/.kerkov/workspaces/2026-10-07-654414a0

I'll perform a loopback-only service inventory: inspect listening TCP/UDP
sockets, map processes and versions where possible, then probe only 127.0.0.1 to
validate exposed services and flag concrete risks. I will not scan non-loopback
interfaces.

→ Executing 2 tool(s): skill_lookup, exec

skill_lookup  load localhost-scan
exec  ss -lntp / ss -lnup / ip -brief addr show lo
  === TCP LISTENERS ===
  LISTEN 0 511 127.0.0.1:40229 0.0.0.0:* users:(("node",pid=15815,fd=19))
  === UDP LISTENERS ===
  UNCONN 0 0 127.0.0.1:323 0.0.0.0:*
  UNCONN 0 0 [::1]:323 [::]:*

→ Executing 2 tool(s): exec, exec

exec  nmap -n -Pn -sT -sV -sC -p- --version-light 127.0.0.1
  Not shown: 65534 closed tcp ports (conn-refused)
  PORT      STATE SERVICE VERSION
  40229/tcp open  unknown
  |   GetRequest:
  |     HTTP/1.1 403 Forbidden
  |     Forbidden.

exec  ps -ww -o user,pid,ppid,lstart,args -p 15815
  kali 15815 15811 ... node /home/kali/.vscode-server/bin/<commit>/out/server-main.js
  --host=127.0.0.1 --port=0 --connection-token=[redacted] --use-host-proxy ...

→ Executing 2 tool(s): exec, exec   (identify the UDP/323 listener)

exec  nmap -n -Pn -sU -sV --version-light -p 323 127.0.0.1
  PORT    STATE         SERVICE VERSION
  323/udp open|filtered unknown

→ Executing 3 tool(s): ledger, ledger, ledger
  Recorded to hosts ledger as entry #1.
  Recorded to services ledger as entry #2.
  Recorded to services ledger as entry #3.

## Loopback service enumeration

Scanned 127.0.0.1 only, including all 65,535 TCP ports and the locally observed
UDP listener.

| Endpoint             | Identification                | Exposure           | Risk |
| 127.0.0.1:40229/tcp  | VS Code Server / Node.js HTTP | IPv4 loopback only | Low  |
| 127.0.0.1:323/udp    | Unknown UDP service           | loopback + ::1     | Info |

- A generic HTTP request to 40229 returned 403 Forbidden — access controls are
  active. TLS to the port produced HTTP 400, so it is plain HTTP, not HTTPS.
- The connection token is visible in the process command line to users allowed
  to inspect that process.
- UDP 323 is conventionally chrony's command socket, but no chrony package or
  service was visible, so that identification is not confirmed.
```

You stay in the loop the whole way: approve, deny, or type a new instruction to
redirect it.

## 4. Record what matters

Confirmed facts go into the **findings ledger**, which stays in context even as
the conversation is compacted. You can ask for it directly:

```text
kerkov › Add anything notable to the findings ledger, then summarize what we know.
```

```text
→ Executing 2 tool(s): ledger, ledger
  Recorded to findings ledger as entry #4.
  Recorded to findings ledger as entry #5.

## Ledger updated

1. Low — VS Code Server connection token exposed in process command line
   - 127.0.0.1:40229/tcp; server is loopback-only and rejects generic
     unauthenticated requests with HTTP 403. The auth token is supplied as a
     command-line argument, visible to users allowed to inspect the process.
   - No authentication bypass was demonstrated. CWE-214.

2. Informational — Unidentified UDP service on port 323
   - 127.0.0.1:323/udp, also observed on ::1. Nmap reported open|filtered and
     could not fingerprint it; the owning process was not visible with current
     privileges.

- A complete scan of all 65,535 TCP ports found only port 40229 open.
- No externally bound services were identified in this loopback-scoped review.
- No critical, high, or medium-severity vulnerability was confirmed.
```

See [Memory: recall and the ledger](../guides/memory.md) for what persists and
how to query bulky tool output later.

## 5. Useful launch patterns

You'll often start Kerkov in a particular posture or resume earlier work:

```bash
# Passive discovery only — no exploitation
kerkov --mode recon

# Resume the most recent engagement
kerkov --continue

# Resume a specific engagement
kerkov --resume <engagement-id>
```

## 6. Leave and come back

Exit with `/q`. Kerkov prints the command to resume this exact engagement.
Everything — history, findings, and captured tool output — is saved locally
under `~/.kerkov/engagements/`.

## Where to go next

- [Local host scan](../examples/localhost-scan.md) — the same target, driven by
  the `localhost-scan` skill instead of a free-form instruction.
- [Modes and autonomy](../guides/modes-and-autonomy.md) — match posture and
  hands-off level to the task.
- [Approvals](../guides/approvals.md) — make safe, repetitive work frictionless.
- [Engagements](../guides/engagements.md) — resume, list, and manage saved work.
- [Slash commands](../reference/slash-commands.md) — everything you can type.
