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
      bins: [nmap, curl]
---

# HackTheBox Machine Solve

Use this skill after `htb-machine-setup` has reported `status: ready` with
a routable target IP. This skill defines the **process** for solving an
HTB machine — phases, decision points, and how to submit flags through
the API. It does not prescribe tools or exploit techniques; pick those
based on what enumeration finds and what's installed on the host.

## Inputs

Required from the previous skill (or the user):

- `target_ip` — the routable IP from `htb-machine-setup`
- `machine_id` — the integer HTB id used for flag submission
- `os` — `Linux` or `Windows` (changes flag paths)

If any are missing, stop and load `htb-machine-setup` first.

## Operating principles

- **Ground every move in evidence.** Enumerate, read the output, decide.
  Don't run exploits without confirming the target version supports them.
- **Prefer non-destructive verification first.** Banner grab and
  metadata reads before anything that changes state.
- **Stay on the target.** Anything outside `target_ip` is out of scope:
  no scanning the user's host, no pivoting to other HTB infrastructure,
  no probing the HTB platform itself.
- **Surface dead ends.** If a phase stalls for several attempts,
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

If the foothold is a shell, decide whether to keep it as a one-shot
command channel (`exec`) or stabilize it into a managed session
(`exec ... background=true` + `process`). The latter is usually worth
it for any non-trivial work.

### 4. Capture user.txt

The user flag is a 32-character lowercase hex string (`[a-f0-9]{32}`).

- **Linux** — typically `/home/<user>/user.txt`
- **Windows** — typically `C:\Users\<user>\Desktop\user.txt`

Read the file, validate the format, then submit via the API:

```
http profile=htb method=POST path=/api/v4/machine/own \
  json={"id": <machine_id>, "flag": "<flag>", "difficulty": <1-10>}
```

`difficulty` is HTB's 1–10 self-rating scale. If the user has not
expressed an opinion, default to **5** and mention it in the output.
Surface the API's response message verbatim on failure.

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

- If `target_ip` is unreachable, stop and re-run `htb-machine-setup`.
- If a flag submission fails, check the format first (32 hex chars, no
  surrounding whitespace). HTB's response message tells you whether
  it's wrong-flag or already-owned.
- If multiple exploitation paths look viable, pick the one with the
  fewest moving parts and clearest preconditions. Prefer authenticated,
  deterministic techniques over noisy brute force.
- If the machine state changes mid-solve (rate-limited, retired,
  rebooted), surface and pause. Do not paper over.
- After capturing root, do not pivot, install persistence, or scan
  outward. End at submission and report.

## Cleanup

When done (success or give-up), terminate the machine via
`htb-machine-setup`'s cleanup, or directly:

```
http profile=htb method=POST path=/api/v4/vm/terminate \
  json={"machine_id": <machine_id>}
```

Then kill the openvpn process started by setup:

```
process action=kill session_id=<vpn_session_id>
```
