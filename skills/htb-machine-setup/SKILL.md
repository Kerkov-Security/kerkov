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
2. `HTB_API_TOKEN` set in `.env` (generate at
   https://app.hackthebox.com/profile/settings → App Tokens).
3. `openvpn`, `ping`, and `python3` on `PATH`.
4. Kerkov launched with elevated privileges (`sudo kerkov ...` on
   Linux/macOS, elevated terminal on Windows). Enforced by the runtime
   via `requires.elevated: true`.

VPN configs are downloaded automatically. `HTB_VPN_CONFIG` (absolute path
to a `.ovpn`) is honoured as an explicit override.

## Procedure

Every `python scripts/htb_service.py ...` invocation prints JSON to stdout on
success and a human-readable explanation to stderr on failure. Exit codes
carry meaning — see Decision Rules.

### 1. Confirm token and detect tier

```
exec command="python scripts/htb_service.py whoami" timeout=30
```

Output: `{"account_tier": "vip"|"free", ...}`. Record the tier — it gates
retired machines and VPN servers.

### 2. Resolve the machine

```
exec command="python scripts/htb_service.py find <name>" timeout=30
```

Single match: stdout JSON carries `id`, `name`, `os`, `difficulty`,
`retired`. If `retired: true` and `account_tier == "free"`, refuse:
"Machine `<name>` is retired and requires VIP. Pick an active machine or
upgrade."

### 3. Check for an already-spawned machine

```
exec command="python scripts/htb_service.py active" timeout=30
```

If something else is running and it is **not** the requested machine, ask
the user before terminating. On approval:

```
exec command="python scripts/htb_service.py terminate <other_id>" timeout=30
```

If the requested machine is already active, skip to Step 6.

### 4. Spawn

```
exec command="python scripts/htb_service.py spawn <machine_id>" timeout=30
```

### 5. Acquire the VPN config

If `$HTB_VPN_CONFIG` is set, use it and skip to Step 6.

If `data/htb/vpn/<server_name>.ovpn` already exists from a prior run,
reuse it.

Otherwise:

```
exec command="python scripts/htb_service.py vpn-servers --tier <free|vip>" timeout=30
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
exec command="python scripts/htb_service.py vpn-switch <server_id>" timeout=30
```

Then download (use the chosen server, or `assigned` if you didn't switch):

```
exec command="python scripts/htb_service.py vpn-download <server_id> --output data/htb/vpn/<server_name>.ovpn" timeout=60
```

Pass the tier from Step 1. For VIP users default to a VIP server (lower
contention, retired works there); fall back to free if none.

### 6. Bring the VPN up

The script can't run openvpn — it's a long-lived daemon. Drive it through
the `exec` and `process` tools. **Pass `unbounded=true`**: openvpn goes
quiet after the handshake, so the default 120s / 60s timeouts would kill
the tunnel. `unbounded=true` disables both. Sudo is not needed in the
command itself: the runtime has already verified kerkov is elevated.

```
exec command="openvpn --config <resolved_ovpn_path>" background=true pty=true unbounded=true
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
exec command="python scripts/htb_service.py wait-ip --timeout 180" timeout=200
```

Stdout: `{"ip": "...", "machine_id": ..., "name": "..."}`. Then prove the
tunnel routes:

```
exec command="ping -c 3 <target_ip>" timeout=15
```

If pings fail but the API says the box is up, suspect the VPN first —
re-check the openvpn log via `process action=log`.

## Handoff

When everything is verified, summarize:

- `status`: `ready` | `failed`
- `machine`: `{ id, name, os, difficulty }`
- `target_ip`: e.g. `10.10.10.3`
- `vpn_config_path`: resolved `.ovpn` path
- `vpn_session_id`: openvpn session id (kill with `process action=kill`)
- `cleanup`: the exact terminate call

Hand off to `htb-machine-solve` (or whatever follows) so it doesn't redo
this work.

## Cleanup

```
exec command="python scripts/htb_service.py terminate <machine_id>" timeout=30
process action=kill session_id=<vpn_session_id>
```

## Decision Rules

Script exit codes:

- **2 (auth)** — token missing/expired. Tell the user to regenerate it at
  https://app.hackthebox.com/profile/settings and restart kerkov.
- **3 (not found)** — no match for the machine name. Stop.
- **4 (ambiguous)** — multiple matches; stderr lists candidates and
  stdout has a `candidates` array. Present and let the user choose, then
  re-call `find` with the exact name (or skip ahead to `spawn` with the
  chosen id).
- **5 (cooldown)** — HTTP 429. stderr carries the `Retry-After`. Surface
  verbatim; never auto-retry.
- **6 (blocked)** — 403 + Cloudflare HTML. Surface; suggest
  status.hackthebox.com.
- **7 (timeout)** — `wait-ip` did not see an IP within the deadline. Ask
  the user whether to keep waiting or terminate.
- **8 (api)** — other HTTP/JSON error; stderr has the status and body.

Other rules:

- No machine named → ask before doing anything.
- Retired machine + free tier → refuse; do not attempt to spawn.
- Active machine that isn't the requested one → ask before terminating.
- Multiple eligible VPN servers → present and let the user choose.
- Persistent TLS handshake errors on openvpn up → delete the cached
  `.ovpn` and re-download once before surfacing failure.
- Ping fails but API says the box is up → suspect the VPN first; do not
  proceed to exploitation until reachability is proven.

## References

- HTB App Tokens: https://app.hackthebox.com/profile/settings
- HTB platform status: https://status.hackthebox.com
- HTB subscription tiers: https://help.hackthebox.com (search "subscription")
