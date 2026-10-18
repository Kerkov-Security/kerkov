---
name: htb-machine-setup
description: "Set up a HackTheBox machine (free or VIP): connect VPN, spawn the box, verify reachability, and report target details for follow-on exploitation"
user-invocable: true
disable-model-invocation: false
metadata:
  kerkov:
    category: lab/htb
    tactic: resource-development
    mitre: [T1583]
    difficulty: beginner
    platforms: [linux, macos, windows]
    tags: [htb, hackthebox, vpn, machine, lab, environment, setup]
    products: [hackthebox]
    lab_sources: [hackthebox]
    safe_verification: true
    requires:
      bins: [openvpn, ping, python3]
      elevated: true
      credentials: [HTB_API_TOKEN]
---

# HackTheBox Machine Setup

Bring a HackTheBox machine online: VPN up, machine spawned, IP routable,
ready for `htb-machine-solve` or any follow-on exploitation work. Supports
both free and VIP tiers.

The mechanical HTB API calls live in `scripts/htb_service.py`. This skill drives
that script and handles the human-in-loop decisions: disambiguating
matches, choosing VPN servers, terminating active machines, and
interpreting errors.

## Inputs

- **Machine name** (e.g. `Lame`, `Bashed`). Case-insensitive.

If the user did not name a machine, ask before doing anything.

## Prerequisites

1. HackTheBox account (free or VIP).
2. An HTB App Token (generate it at
   https://app.hackthebox.com/profile/settings → App Tokens). Kerkov requests
   it with masked input when this skill is loaded and persists it outside
   `.env`.
3. `openvpn`, `ping`, and `python3` on `PATH`.
4. Kerkov launched with elevated privileges (`sudo -E uv run kerkov ...`
   from the repo, or `sudo kerkov ...` for a global install, on Linux/macOS;
   elevated terminal on Windows). Enforced by the runtime via
   `requires.elevated: true`.

VPN configs are downloaded automatically. `HTB_VPN_CONFIG` (absolute path
to a `.ovpn`) is honoured as an explicit override.

## Procedure

The HTB helper runs as a subprocess, so its token must be persisted: configure
it with `/auth set HTB_API_TOKEN` and accept the default persistence option.
Do not use `--session` for this credential, and never place the token value in a
command. Every `python3 '<skill_dir>/scripts/htb_service.py' ...` invocation uses
`mode=one_shot`. The helper reads the protected credential store directly,
prints JSON to stdout on success, and prints a human-readable explanation to
stderr on failure. Exit codes carry meaning — see Decision Rules.

Resolve `<skill_dir>` from the skill load header to an absolute path before
running commands; the current directory is the engagement workspace. Helper
timeouts allow its 60-second HTTP timeout to finish.

### 1. Confirm token and detect tier

```
exec command="python3 '<skill_dir>/scripts/htb_service.py' whoami" mode=one_shot timeout=75 no_output_timeout=75
```

Output: `{"account_tier": "vip"|"free", ...}`. Record the tier as a VPN-selection
hint; the current server assignment and actual API access responses take priority.

### 2. Resolve the machine

```
exec command="python3 '<skill_dir>/scripts/htb_service.py' find <name>" mode=one_shot timeout=75 no_output_timeout=75
```

Single match: stdout JSON carries `id`, `name`, `os`, `difficulty`,
`retired`. Do not reject a machine solely because it is retired and the account
is free: HTB offers a rotating free selection of retired machines. Attempt the
requested spawn and surface an actual subscription/access rejection if returned.

### 3. Check for an already-spawned machine

```
exec command="python3 '<skill_dir>/scripts/htb_service.py' active" mode=one_shot timeout=75 no_output_timeout=75
```

If something else is running and it is **not** the requested machine, ask
the user before terminating. On approval:

```
exec command="python3 '<skill_dir>/scripts/htb_service.py' terminate <other_id>" mode=one_shot timeout=75 no_output_timeout=75
```

If the requested machine is already active, skip spawning and continue at Step 5.
Reuse its healthy VPN session in Step 6 when available.

### 4. Spawn

```
exec command="python3 '<skill_dir>/scripts/htb_service.py' spawn <machine_id>" mode=one_shot timeout=75 no_output_timeout=75
```

### 5. Acquire the VPN config

If `$HTB_VPN_CONFIG` is set, use it and skip to Step 6.

If `data/htb/vpn/<server_name>.ovpn` already exists from a prior run,
reuse it.

Otherwise:

```
exec command="python3 '<skill_dir>/scripts/htb_service.py' vpn-servers --tier <free|vip>" mode=one_shot timeout=75 no_output_timeout=75
```

Output shape: `{"assigned": <server>|null, "servers": [<other_server>, ...]}`.

**Prefer `assigned`.** HTB only serves an `.ovpn` for the server the
account is currently assigned to — picking any other id without switching
first returns `404 "You are not assigned to this VPN Server."` Use the
assigned server unless the user explicitly asks for a different region or
tier, or `assigned.full` is true.

If you must use a different server (user asked, or `assigned` is null /
full), switch first:

```
exec command="python3 '<skill_dir>/scripts/htb_service.py' vpn-switch <server_id>" mode=one_shot timeout=75 no_output_timeout=75
```

Then download (use the chosen server, or `assigned` if you didn't switch):

```
exec command="python3 '<skill_dir>/scripts/htb_service.py' vpn-download <server_id> --output data/htb/vpn/<server_name>.ovpn" mode=one_shot timeout=75 no_output_timeout=75
```

Pass the tier from Step 1. Prefer the assigned server; do not switch solely
because a cached tier label differs. Current free and VIP+ accounts both have
dedicated instances.

The helper saves VPN configs with owner-only permissions on POSIX because they
contain private keys. Keep their contents out of chat and tool output.

### 6. Bring the VPN up

If the handoff already includes a healthy VPN session using the selected config,
reuse it and record that it predates this setup. Otherwise start a session below
and record that this setup created it. Do not start a duplicate tunnel.

The script can't run openvpn — it's a long-lived daemon. Drive it through
the `exec` and `process` tools using `mode=daemon`, which backgrounds the
process and disables both the overall and no-output timeouts (openvpn
goes quiet after the handshake, so the default 120s / 60s timeouts would
kill the tunnel). Sudo is not needed in the command itself: the runtime
has already verified kerkov is elevated.

```
exec command="openvpn --config <resolved_ovpn_path>" mode=daemon
```

Capture the `session_id`, then tail the log:

```
process action=log session_id=<id> limit=80
```

Wait for `Initialization Sequence Completed`. On `AUTH_FAILED` or repeated
TLS handshake errors, kill the session, delete the cached `.ovpn`, and
re-run Step 5 once:

```
process action=kill session_id=<id>
```

### 7. Wait for the target IP and verify reachability

```
exec command="python3 '<skill_dir>/scripts/htb_service.py' wait-ip --timeout 180" mode=one_shot timeout=200 no_output_timeout=200
```

Stdout: `{"ip": "...", "machine_id": ..., "name": "..."}`. Check that the
returned machine ID matches the requested target before using its IP. Then prove
the tunnel routes:

```
exec command="ping -c 3 <target_ip>" mode=one_shot timeout=15
```

If pings fail but the API says the box is up, re-check the openvpn log via
`process action=log`. ICMP may be filtered: if nmap is available, use
`nmap -n -Pn -sT -p 22,80,443 <target_ip>` to check for a TCP response before
declaring the target unreachable. On Windows use `ping -n 3 <target_ip>`.

### 8. Record the target and confirmed network access

Before returning the handoff, use `ledger` to add or reuse the target IP in
`targets`, with the machine name and ID. This makes the machine visible in the
network map even when only ICMP reachability is known.

For each service with a successful TCP/UDP response or connection (for example,
an SSH banner, `nc` connection, or HTTP response), immediately add a
`connections` entry: `relation="reaches"`, `provenance="observed"`, the target
resource ID, actual protocol and destination port, and the confirming tool
result's `artifact_ref`. For local checks use `source_vantage="Kerkov runner"`.
Omit `observed_at` to use the evidence timestamp. Reuse existing identities.

Writing only `hosts`/`services`, or mentioning access in the handoff, does not
create a map route. Record each confirmed service, including HTTP and HTTPS
separately when both respond. A scan listing, successful spawn, started VPN, or
ping alone does not establish a TCP/UDP service connection; do not invent its
port or a VPN gateway hop. If the ledger is unavailable, report that limitation.

## Handoff

When everything is verified, summarize:

- `status`: `ready` | `failed`
- `machine`: `{ id, name, os, difficulty }`
- `target_ip`: e.g. `10.10.10.3`
- `vpn_config_path`: resolved `.ovpn` path
- `vpn_session_id`: openvpn session id, and whether setup created or reused it
- `cleanup`: user preference (default: keep running) and the exact terminate call
  for optional later use; this handoff does not authorize running it

Hand off to `htb-machine-solve` (or whatever follows) so it doesn't redo
this work.

## Optional cleanup

Keep the machine and VPN running for the solver. Execute cleanup only when the
user explicitly chooses it; setup/solve success, failure, or "stop solving" does
not authorize teardown. Honor an earlier explicit cleanup choice without asking
again. Otherwise ask whether to keep running, terminate only the box, or terminate
the box and disconnect this solve's VPN. No answer means leave running.

Run the terminate command only for the machine the user selected, then check
`active` to confirm it stopped. Kill the VPN session only if disconnecting it was
chosen and its ownership is known; do not kill unrelated/shared VPN sessions.
Preserve solve evidence and report any termination failure.

```
exec command="python3 '<skill_dir>/scripts/htb_service.py' terminate <machine_id>" mode=one_shot timeout=75 no_output_timeout=75
exec command="python3 '<skill_dir>/scripts/htb_service.py' active" mode=one_shot timeout=75 no_output_timeout=75
```

Only when VPN disconnection was also selected:

```
process action=kill session_id=<vpn_session_id>
```

## Decision Rules

Script exit codes:

- **2 (auth)** — token missing/expired. Tell the user to regenerate it at
  https://app.hackthebox.com/profile/settings and restart kerkov.
- **3 (not found)** — no match for the machine name. Stop.
- **4 (cooldown)** — HTTP 429. stderr carries the `Retry-After`. Surface
  verbatim; never auto-retry.
- **5 (blocked)** — 403 + Cloudflare HTML. Surface; suggest
  status.hackthebox.com.
- **6 (timeout)** — `wait-ip` did not see an IP within the deadline. Ask
  the user whether to keep waiting or terminate.
- **7 (api)** — other HTTP/JSON error; stderr has the status and body.
  Lifecycle requests also use this code when HTB rejects the operation in a
  successful HTTP response or does not explicitly confirm acceptance. Check
  `active` or the assigned VPN server before retrying; HTTP 200 alone does not
  prove the machine spawned, terminated, or switched VPN servers.

Other rules:

- No machine named → ask before doing anything.
- Subscription/access rejection from HTB → surface it; do not infer it solely
  from a retired flag or cached tier.
- Active machine that isn't the requested one → ask before terminating.
- Multiple eligible VPN servers → present and let the user choose.
- Persistent TLS handshake errors on openvpn up → delete the cached
  `.ovpn` and re-download once before surfacing failure.
- Ping fails but API says the box is up → suspect the VPN first; do not
  proceed to exploitation until reachability is proven.

## References

- HTB App Tokens: https://app.hackthebox.com/profile/settings
- HTB platform status: https://status.hackthebox.com
- HTB machine access and controls (checked 2026-09-06):
  https://help.hackthebox.com/en/articles/5185338-how-to-play-machines-on-hack-the-box
