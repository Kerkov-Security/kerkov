# Local host scan

The gentlest way to see Kerkov work a target end to end: point it at your own
machine. This walkthrough runs the shipped [`localhost-scan`](../guides/skills.md)
skill, which enumerates every listening TCP port on `127.0.0.1` (and `::1`),
fingerprints what it finds, attributes each socket to its owning process, and
records the result in the [findings ledger](../guides/memory.md).

!!! danger "Authorized use only"
    This example scans `127.0.0.1` and `::1` only. Pointing Kerkov at another
    host is your responsibility — do it only where you own the target or have
    explicit written permission.

## Prerequisites

- Kerkov [installed](../get-started/install.md) with a working provider.
- `nmap` on your `PATH` (the skill declares it as a required binary).

## The command

The skill is invoked with the [`/skill`](../reference/slash-commands.md) command.
This transcript was captured non-interactively so it is reproducible and
auto-approves its own tool calls — the interactive equivalent is to start
`kerkov --mode recon` and type `/skill localhost-scan` at the prompt:

```bash
kerkov --mode recon --no-welcome --verbosity standard \
  --message "/skill localhost-scan"
```

## What it did

Output is real, captured from the run above. The only redaction is the VS Code
Server connection token, replaced with `[redacted]`.

```text
Configured Codex model 'gpt-5.4' is unavailable for this ChatGPT account; using
'gpt-5.6-sol'.
✓ Connected to gpt-5.6-sol
Loaded skill: localhost-scan
Workspace: /home/kali/.kerkov/workspaces/2026-10-07-a6de9031

I'll inventory local listeners and owning processes, then run full
TCP-connect/version scans against both IPv4 and IPv6 loopback. I'll compare bind
addresses to distinguish loopback-only services from those exposed on all
interfaces; UDP coverage will come from the local socket table to avoid
privileged intrusive scanning.

→ Executing 3 tool(s): exec, exec, exec

exec  ss -lntp / ss -lnup / uname -a / ip -brief address
  === SYSTEM ===
  Linux BY2022A98604034 6.18.40.1-microsoft-standard-WSL2 #1 SMP PREEMPT_DYNAMIC
  Fri Jul 31 22:12:15 UTC 2026 x86_64 GNU/Linux
  === INTERFACES ===
  lo     UNKNOWN  127.0.0.1/8 ::1/128
  eth0   UP       172.24.114.99/20 fe80::215:5dff:fefe:3289/64
  === TCP LISTENERS ===
  LISTEN 0 511 127.0.0.1:40229 0.0.0.0:* users:(("node",pid=15815,fd=19))
  === UDP LISTENERS ===
  UNCONN 0 0 127.0.0.1:323 0.0.0.0:*
  UNCONN 0 0 [::1]:323 [::]:*

exec  nmap -n -Pn -sT -sV -p- --version-light --reason 127.0.0.1
  Nmap scan report for 127.0.0.1
  Host is up, received user-set (0.000073s latency).
  Not shown: 65534 closed tcp ports (conn-refused)
  PORT      STATE SERVICE REASON  VERSION
  40229/tcp open  unknown syn-ack
  [ nmap could not fingerprint the service; it returns HTTP 403 Forbidden ]

exec  nmap -6 -n -Pn -sT -sV -p- --version-light --reason ::1
  All 65535 scanned ports on ::1 are in ignored states.
  Not shown: 65535 closed tcp ports (conn-refused)

→ Executing 4 tool(s): exec, exec, exec, exec

exec  nmap -sT -sV -sC --script=http-headers,http-methods,http-title -p 40229 127.0.0.1
  PORT      STATE SERVICE VERSION
  40229/tcp open  unknown
  |   FourOhFourRequest, GetRequest:
  |     HTTP/1.1 403 Forbidden
  |   HTTPOptions, RTSPRequest:
  |     HTTP/1.1 405 Method Not Allowed
  |_    Unsupported method OPTIONS

exec  ps -p 15815 -o pid,ppid,user,lstart,etime,comm,args --no-headers
  15815 15811 kali ... node
  /home/kali/.vscode-server/bin/<commit>/node
  /home/kali/.vscode-server/bin/<commit>/out/server-main.js
  --host=127.0.0.1 --port=0 --connection-token=[redacted] --use-host-proxy ...

exec  curl --include --request OPTIONS http://127.0.0.1:40229/
  HTTP/1.1 405 Method Not Allowed
  Content-Type: text/plain
  Connection: keep-alive

exec  nmap -n -Pn -sU -p 323 --version-light 127.0.0.1
  PORT    STATE         SERVICE
  323/udp open|filtered unknown

→ Executing 2 tool(s): exec, exec   (confirm the port is loopback-only)

exec  nmap -n -Pn -sT -p 40229 --reason 172.24.114.99
  PORT      STATE  SERVICE REASON
  40229/tcp closed unknown conn-refused   ← not exposed on the WSL interface

→ Executing 3 tool(s): ledger, ledger, ledger
  Recorded to hosts ledger as entry #1.
  Recorded to services ledger as entry #2.
  Recorded to services ledger as entry #3.

## Localhost reconnaissance results

Host: Linux WSL2 (6.18.40.1-microsoft-standard-WSL2)
Interfaces: 127.0.0.1, ::1, and 172.24.114.99

| Endpoint             | Service                         | Exposure           | Risk |
| 127.0.0.1:40229/tcp  | VS Code Server HTTP / Node.js   | IPv4 loopback only | Info |
| 127.0.0.1:323/udp    | Unidentified UDP listener       | IPv4 loopback only | Low  |
| [::1]:323/udp        | Same unidentified UDP listener  | IPv6 loopback only | Low  |

- A complete scan of all 65,535 TCP ports found only 40229/tcp open.
- The same port was closed on 172.24.114.99, confirming it is not exposed
  through the WSL network interface.
- The VS Code Server command line contains a connection token; it is
  intentionally omitted. Local users with sufficient process-inspection
  permissions may be able to read such command-line secrets.
- Port 323/udp is conventionally chrony's command socket, but no chronyd
  process or installed chrony package was found, so the identity is unconfirmed.

No externally exposed service or confirmed vulnerability was identified.
```

## What to take away

- **One skill, a full recon pass.** `/skill localhost-scan` gave the model a
  procedure; the model chose the exact commands, read each result, and decided
  what to do next — a full-range TCP sweep, targeted service fingerprinting,
  process attribution, and a loopback-vs-interface exposure check.
- **It proves exposure rather than assuming it.** Finding `40229/tcp` open on
  loopback, it re-scanned the same port on the WSL `eth0` address and showed it
  `closed` there — the difference between "listening" and "exposed".
- **Facts land in the ledger.** The host and two services were written to the
  [findings ledger](../guides/memory.md), so they stay in context even after the
  conversation is compacted.

## Related pages

- [Skills](../guides/skills.md) and the [`skill_lookup` tool](../reference/tools/skill.md)
- [Exec and process tools](../reference/tools/exec-process.md)
- [Memory — recall and the ledger](../guides/memory.md)
- [Your first engagement](../get-started/first-engagement.md) — the same target,
  worked conversationally rather than through a skill.
