# Delegating to agents

When a task splits into independent pieces, the master can hand each piece to a
separate [agent](../guides/agents.md) with the [`delegate`](../reference/tools/delegate.md)
tool. The agents run in parallel, each in its own isolated context, and the
master collects their reports with [`agent_wait`](../reference/tools/delegate.md).
This example fans three read-only, loopback-only recon sub-tasks out at once and
reassembles their findings into one summary.

!!! danger "Authorized use only"
    Every sub-task below is read-only and scoped to the local host. Delegation
    multiplies what the model can do at once — keep each agent's instruction
    inside the same scope you would allow the master.

## Prerequisites

- Kerkov [installed](../get-started/install.md) with a working provider.
- Nothing else — the sub-tasks use `ss`, `/etc/os-release`, `uptime`, `who`,
  and presence checks for common hardening tools.

## The command

```bash
kerkov --mode recon --no-welcome --verbosity standard \
  --message "Delegate three independent agents to gather local host facts in \
parallel (loopback/read-only only): (1) enumerate listening TCP sockets on \
127.0.0.1 and their owning processes with ss; (2) collect host facts — kernel, \
distro from /etc/os-release, uptime, and logged-in users; (3) check whether \
common hardening tools are present (ufw, fail2ban, apparmor) and their status. \
Give each agent a short descriptive name. Then use agent_wait to collect all \
three reports and give me one combined summary."
```

## What it did

Output is real, captured from the run above.

```text
Configured Codex model 'gpt-5.4' is unavailable for this ChatGPT account; using
'gpt-5.6-sol'.
✓ Connected to gpt-5.6-sol
Workspace: /home/kali/.kerkov/workspaces/2026-10-07-2d7c9a16

I'll launch three narrowly scoped recon agents in parallel: one for loopback TCP
listeners and process ownership, one for basic host facts, and one for
hardening-tool presence/status. All checks will be read-only and limited to the
local host.

→ Executing 1 tool(s): delegate

delegate  3 tasks
  Spawned 3 agent(s) (detached). They run in the background while you keep working.
  - loopback-listeners (a1, recon): ... read-only enumeration of listening TCP sockets ...
  - host-facts (a2, recon): ... kernel information, distro, uptime, logged-in users ...
  - hardening-status (a3, recon): ... whether ufw, fail2ban, and AppArmor are present ...
  Call agent_wait(ids=["a1","a2","a3"]) to block until they report, or continue
  working — finished reports are delivered to you automatically at your next turn.

The three read-only agents are running independently. I'll now wait for all
reports, then consolidate the loopback sockets, host facts, and hardening status.

→ Executing 1 tool(s): agent_wait

agent_wait  ids=["a1","a2","a3"] timeout_s=120

  ### loopback-listeners — recon [completed] (id: a1, 3,374 tokens)
  (engagement: 2026-10-07-ac686edd)
  FINDINGS:
  One TCP LISTEN socket was visible specifically bound to 127.0.0.1:
  - 127.0.0.1:40229  LISTEN  node (PID 15815; fd 19)

  ### host-facts — recon [completed] (id: a2, 4,121 tokens)
  (engagement: 2026-10-07-b8a30b2d)
  FINDINGS:
  Kernel: Linux 6.18.40.1-microsoft-standard-WSL2 x86_64
  Distro: Kali GNU/Linux Rolling 2026.2 (kali-rolling, ID_LIKE debian)
  Uptime: up 55 min, 1 user, load 0.50 0.32 0.42
  Logged-in users: kali on pts/1 since 2026-10-06 21:14

  ### hardening-status — recon [completed] (id: a3, 12,109 tokens)
  (engagement: 2026-10-07-803ca660)
  FINDINGS:
  UFW not installed; Fail2ban not installed; AppArmor user-space tooling not
  installed — kernel module present but disabled (enabled=N).

## Combined local host summary

### 1. Loopback TCP listeners
| Address          | State  | Process                      |
| 127.0.0.1:40229  | LISTEN | node, PID 15815, FD 19       |

### 2. Host facts
- Hostname: BY2022A98604034 (WSL2)
- Kernel: Linux 6.18.40.1-microsoft-standard-WSL2, x86_64
- Distribution: Kali GNU/Linux Rolling 2026.2 (Debian-based)
- Uptime: 55 minutes; load 0.50 / 0.32 / 0.42
- Logged-in user: kali on pts/1 since 2026-10-06 21:14

### 3. Hardening tools
| Tool      | Presence                     | Status                               |
| UFW       | Not installed                | No executable, config, or service    |
| Fail2ban  | Not installed                | No package, executable, or service   |
| AppArmor  | User-space tooling absent    | Kernel module present but disabled   |

All checks were local, loopback/read-only, and performed without privilege
escalation. No permission-denied conditions were encountered.
```

## What to take away

- **`delegate` returns immediately.** The three agents were spawned *detached* —
  the tool came back with their handles (`a1`, `a2`, `a3`) right away, leaving
  the master free. Each agent got a `name` the master chose in the same call.
- **`agent_wait` collects.** The master blocked on all three ids and received
  one report each, with the agent's own token spend and
  [engagement id](../guides/engagements.md) attached. Those ids are how you later
  [`recall`](../reference/tools/recall.md) the full raw output behind an agent's
  summary.
- **Isolation, not shared chatter.** Each agent reasoned in its own context and
  reported a conclusion — the master never saw their intermediate tool output,
  only the findings. That is what keeps a fan-out from flooding the master's
  context.
- **Reports are delivered even if you forget to wait.** A detached agent's report
  is appended to the master at its next turn; in a one-shot `--message` run,
  Kerkov waits for outstanding agents before exiting so paid-for work is never
  stranded.

## Related pages

- [Agents](../guides/agents.md) — profiles, budgets, and how spawning works.
- [`delegate` and `agent_wait`](../reference/tools/delegate.md)
- [The agent subsystem](../concepts/agent-subsystem.md) — detachment, supervision,
  and report delivery in depth.
- [Non-interactive use](../guides/non-interactive.md) — the `--message` form used here.
