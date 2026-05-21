---
name: htb-machine-solve
description: "Solve a HackTheBox machine end-to-end: enumerate, gain foothold, capture user.txt, escalate, capture root.txt, and submit both flags"
user-invocable: true
disable-model-invocation: false
metadata:
  kerkov:
    category: lab/htb
    tactic: initial-access
    mitre: [T1190, T1078, T1068]
    difficulty: intermediate
    platforms: [linux, macos, windows]
    tags: [htb, hackthebox, machine, exploitation, privesc, flag]
    products: [hackthebox]
    lab_sources: [hackthebox]
    safe_verification: true
    requires:
      bins: [nmap, curl, python3]
---

# HackTheBox Machine Solve

Use this skill after `htb-machine-setup` has reported `status: ready` with
a routable target IP. This skill defines the **process** for solving an
HTB machine — phases, decision points, and how to submit flags through
the API. It does not prescribe tools or exploit techniques; pick those
based on what enumeration finds and what's installed on the host.

## Operating mode

Attempt to solve the machine without user input. Drive enumeration,
foothold, privesc, and flag submission yourself, picking concrete next
steps from what each command actually returns. Only surface to the user
when you're genuinely stalling — a phase that's run multiple distinct
attempts with no new signal, an auth/account problem you can't fix, or
an explicit ambiguity (e.g. multiple equally plausible foothold paths
that differ in blast radius). Brief progress updates between phases are
fine; mid-phase narration is not.

## Inputs

Required from the previous skill (or the user):

- `target_ip` — the routable IP from `htb-machine-setup`
- `machine_id` — the integer HTB id used for flag submission
- `os` — `Linux` or `Windows` (changes flag paths)

If any are missing, auto-load `htb-machine-setup` to acquire them rather
than stopping.

## Bundled script

Flag submission and ownership lookup go through `scripts/htb_service.py`
in this skill's directory (the same wrapper `htb-machine-setup` ships;
each skill carries its own copy so paths stay simple). All paths below
are relative to this skill's `skill_dir` (printed in the load header).

Before starting Phase 1, query ownership state and skip any flag the
account already has:

```
exec command="python scripts/htb_service.py status <machine_id>" timeout=30
```

Output: `{"user_owned": bool, "root_owned": bool, "os": "..."}`. If
`user_owned` is true, skip Phase 4. If `root_owned` is true, skip Phase 6.
If both are already owned, return `status: solved` immediately.

## Operating principles

- **Ground every move in evidence.** Enumerate, read the output, decide.
  Don't run exploits without confirming the target version supports them.
- **Prefer non-destructive verification first.** Banner grab and
  metadata reads before anything that changes state.
- **Stay on the target.** Anything outside `target_ip` is out of scope:
  no scanning the user's host, no pivoting to other HTB infrastructure,
  no probing the HTB platform itself.
- **Surface dead ends, don't bury them.** A phase is stalling when
  multiple distinct attempts produce no new signal (not when one
  attempt fails — pivot to the next vector first). When you do stall,
  summarize what's been tried and ask the user before going deeper.
- **Pick tooling by what's available.** Use `skill_lookup` to find any
  relevant kerkov skill (e.g. for a known vulnerability class), and
  prefer tools already on `PATH` over installing new ones mid-engagement.

## Phases

The order is fixed; the techniques inside each phase are not.

### 1. Recon

Discover the target's attack surface: what ports are open, what services
listen, what versions they advertise. Goal: a short list of services
worth investigating, with version strings where possible.

### 2. Enumeration

For each interesting service, dig into its specifics: routes, shares,
default credentials, accessible endpoints, exposed files, configuration
hints. The output of this phase is a ranked list of plausible attack
vectors.

### 3. Foothold

Pick the most promising vector and turn it into authenticated access or
a shell. Prefer techniques with the fewest moving parts and the clearest
preconditions. Confirm the version actually matches before launching a
public PoC.

Once the foothold is a shell — SSH, reverse shell, `su`, web RCE, any
authenticated access — open it as a managed session, not a stream of
one-shot commands:

```
exec command="sshpass -p '<pw>' ssh -o StrictHostKeyChecking=no <user>@<ip>" \
  mode=session
```

Then drive it through the `process` tool (`paste` text + `submit` to run,
`poll`/`log` to read output). Reasons this matters:

- TTY-dependent commands (`sudo -l`, `su`, `passwd`, `vim`, `less`) only
  work with a real PTY. A fresh `ssh ... 'sudo -l'` fails with
  *"sudo: a terminal is required to read the password"*.
- State persists across commands — cwd, exported env vars, sudo cache,
  shell history.
- One connection, no per-command reconnect overhead.

**Anti-pattern**: spawning `mode=one_shot` `ssh user@host '<command>'` for
each step. It works for `whoami` and `cat`, breaks on anything
interactive, and loses every bit of session state between commands. If
you're about to run a second one-shot SSH against the same host, stop
and open `mode=session` instead.

### 4. Capture user.txt

The user flag is a 32-character lowercase hex string (`[a-f0-9]{32}`).

- **Linux** — typically `/home/<user>/user.txt`
- **Windows** — typically `C:\Users\<user>\Desktop\user.txt`

Read the file, validate the format (`[a-f0-9]{32}`, no whitespace),
then submit:

```
exec command="python scripts/htb_service.py submit <machine_id> <flag>" timeout=30
```

`--difficulty <1-10>` is optional. Omit it unless you want to rate the
box — if you do rate, base it on what the solve actually took (foothold
clarity, privesc steps, dead ends) and mention the rating in the output.

Exit codes:

- **0** — submitted. Stdout JSON includes HTB's `message`.
- **8 (wrong flag)** — re-read the file, check for stray whitespace or a
  truncated read; do not retry without a different value.
- **9 (already owned)** — treat as success; record and move on.

Surface the API's response message verbatim on any other failure.

### 5. Privilege escalation

Enumerate from the foothold context to find a path to a higher-privileged
user (root on Linux, SYSTEM/Administrator on Windows). Look across the
common categories — credentials reused or stored on disk, misconfigured
services or scheduled tasks, group/capability/privilege misuse, kernel
or service vulnerabilities — but let what's actually present on the box
choose the path, not a checklist.

Kernel-level exploits are last resort: noisy and often unstable.

### 6. Capture root.txt

- **Linux** — typically `/root/root.txt`
- **Windows** — typically `C:\Users\Administrator\Desktop\root.txt`

Submit the same way as user.txt.

## Output format

Whether the run succeeds, partially succeeds, or fails, return:

- `status`: `solved` | `partial` | `failed`
- `target_ip`
- `machine_id`
- `phases_completed`: e.g. `[recon, enum, foothold, user]`
- `user_flag_submitted`: bool
- `root_flag_submitted`: bool
- `notable_findings`: short bullet list (services, versions, creds, the
  technique that worked)
- `cleanup`: pointer to the terminate call from `htb-machine-setup`

## Decision rules

- If `target_ip` is unreachable, re-run `htb-machine-setup` once before
  surfacing — VPN drops happen.
- If a flag submission fails, check the format first (32 hex chars, no
  surrounding whitespace). HTB's response message tells you whether
  it's wrong-flag or already-owned.
- If multiple exploitation paths look viable, pick the one with the
  fewest moving parts and clearest preconditions. Prefer authenticated,
  deterministic techniques over noisy brute force.
- If the machine reboots mid-solve, re-run `htb_service.py wait-ip`
  once and resume. If it's rate-limited or retired, surface and pause —
  those need the user.
- After capturing root, do not pivot, install persistence, or scan
  outward. Submit, run cleanup, and report.

## Cleanup

When the run ends — both flags submitted, or you've stalled and the user
confirmed stop — terminate the machine and tear down the VPN without
prompting:

```
exec command="python scripts/htb_service.py terminate <machine_id>" timeout=30
process action=kill session_id=<vpn_session_id>
```
