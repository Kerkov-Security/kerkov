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
      bins: [openvpn, ping]
---

# HackTheBox Machine Setup

Use this skill to bring a HackTheBox machine online: VPN up, machine
spawned, IP routable, ready for `htb-machine-solve` (or any follow-on
exploitation work).

Supports both free-tier and VIP accounts. The account tier is detected
from `/api/v4/user/info` and gates which machines (active vs retired) and
which VPN servers are available.

## Inputs

- **Machine name** (e.g. `Lame`, `Bashed`). Case-insensitive substring
  match against the HTB search index.

If the user did not name a machine, ask before calling any API.

## Prerequisites

This skill talks to HTB through the `htb` HTTP profile. Before running it:

1. The user has a HackTheBox account (free tier or VIP — both work).
2. `HTB_API_TOKEN` is set in `.env`. The token is generated once at
   https://app.hackthebox.com/account-settings (App Tokens). It does not
   expire on a fixed schedule but the user can rotate it any time.
3. The `htb` profile in `config/http_profiles.yaml` is uncommented.
4. `openvpn` and `ping` are on `PATH`.

The VPN config is **downloaded automatically** by this skill, so the user
does not need to fetch one manually. `HTB_VPN_CONFIG` in `.env` is honored
as an explicit override if the user prefers their own `.ovpn` file
(absolute path).

If `HTB_API_TOKEN` is missing or the profile is still commented out, the
`http` tool will report an unknown profile or a 401 — surface that to the
user with the exact remediation ("uncomment the htb block in
config/http_profiles.yaml and set HTB_API_TOKEN in .env, then restart").

## Procedure

The order matters: validate the token, detect the account tier so we know
what's reachable, deal with any already-spawned machine before spawning a
new one, and only declare success once the target IP is actually reachable.

### Step 1. Confirm the token works and detect tier

```
http profile=htb method=GET path=/api/v4/user/info
```

Expected: HTTP 200 with the user object. If 401, the token is invalid or
expired — stop and tell the user to regenerate it.

Inspect `is_vip` (and any related `subscription` / `tier` fields) on the
returned user object and record `account_tier` as either `vip` (any paid
tier — VIP, VIP+, Dedicated) or `free`. This value gates the rest of the
skill: which machine pool to search and which VPN servers are eligible.

### Step 2. Resolve the machine

Active machines are accessible to everyone; retired machines require a
paid subscription.

```
http profile=htb method=GET path=/api/v4/machine/list
```

Pick the entry whose name matches exactly (case-insensitive). If multiple
plausible matches, present the candidates to the user before continuing.

If nothing matches in the active list, fall back to retired:

```
http profile=htb method=GET path=/api/v4/machine/list/retired
```

If the chosen entry is retired:

- `account_tier == vip` → proceed normally; capture the machine details
  and continue to Step 3.
- `account_tier == free` → **stop**. Refuse with: "Machine `<name>` is
  retired and requires a VIP subscription. Pick an active machine, or
  upgrade and rerun."

Capture: `machine_id`, `name`, `os`, `difficulty`, `retired`, `ip` (often
null pre-spawn).

Optionally pull richer metadata for the user-facing summary:

```
http profile=htb method=GET path=/api/v4/machine/profile/<machine_id_or_slug>
```

### Step 3. Check for an already-spawned machine

```
http profile=htb method=GET path=/api/v4/machine/active
```

Free tier permits only one active machine at a time; VIP allows several
in parallel but the user may still want a clean slate. Either way, if the
response shows another machine already running and it is **not** the
requested one, **ask the user** before terminating. If they agree:

```
http profile=htb method=POST path=/api/v4/vm/terminate data="machine_id=<other_id>" headers={"Content-Type": "application/x-www-form-urlencoded"}
```

If the active machine is already the requested one, skip ahead to Step 5.

### Step 4. Spawn the requested machine

The HTB spawn endpoint expects a form-encoded body. Sending a JSON body
gets parsed as empty form data and the call is rejected, so use `data`
with an explicit form Content-Type rather than `json`:

```
http profile=htb method=POST path=/api/v4/vm/spawn data="machine_id=<machine_id>" headers={"Content-Type": "application/x-www-form-urlencoded"}
```

Watch for two specific failures:

- **HTTP 429 / cooldown** — body or `Retry-After` header carries the wait
  in seconds. Surface that to the user verbatim ("Spawn cooldown: wait
  <N>s"); do not retry blindly.
- **403 with Cloudflare HTML** — surface to the user, suggest checking
  https://status.hackthebox.com. Do not fall back to browser automation.

### Step 5. Acquire the VPN config

If `HTB_VPN_CONFIG` is set in the environment, treat it as an explicit
override and skip ahead to Step 6 with that path.

Otherwise, list available VPN servers:

```
http profile=htb method=GET path=/api/v4/lab/list
```

The response groups servers by tier. Filter to the servers the user's
`account_tier` is entitled to (the exact field name varies; expect
something like `assigned`, `friendly_name`, `location`, and a
`tier`/`subscription`/`product` indicator distinguishing free from VIP /
VIP+ / Dedicated):

- `account_tier == free` → free servers only.
- `account_tier == vip` → both VIP and free servers are eligible.
  Default to a VIP server (lower contention, retired-box access works
  there); fall back to free if none are listed.

If only one eligible server is offered, pick it. If multiple are offered,
present the short list (name + region + tier) and ask the user.

Cache lookup before download: if `data/htb/vpn/<server_name>.ovpn`
already exists from a previous run, reuse it unless the user has rotated
their token or asked for a fresh download. The cert/key are embedded in
the file, so a fresh token doesn't invalidate an existing config — but
HTB does occasionally rotate server certs, so surface any TLS handshake
errors during VPN up as a hint to re-download.

Otherwise, download the OVPN body:

```
http profile=htb method=GET path=/api/v4/access/ovpnfile/<server_id>/0
```

The response body is plain `.ovpn` text. Write it to disk via the `write`
tool, defaulting to `data/htb/vpn/<server_name>.ovpn` (create the
directory if missing). The file embeds the user's cert and key, so treat
the path as sensitive — surface it in the handoff but don't log the
contents.

### Step 6. Bring the VPN up

Run openvpn against the resolved config path (downloaded or override) in
the background via the `exec` tool, and follow up with `process`:

```
exec command="openvpn --config <resolved_ovpn_path>" background=true pty=true
```

Capture the returned `session_id`. From here on, manage openvpn through
the `process` tool:

```
process action=log session_id=<id> limit=80
```

Look for the line `Initialization Sequence Completed`. That confirms the
tunnel is up. If openvpn asks for credentials (some `.ovpn` files embed
them, some don't), surface the prompt to the user — never invent
credentials.

If the log shows `AUTH_FAILED` or repeated TLS handshake errors, kill the
session and re-download the `.ovpn` (delete the cached file first so
Step 5 fetches a fresh one):

```
process action=kill session_id=<id>
```

### Step 7. Wait for the target IP to become routable

After spawn, the machine takes time to boot. Poll the active endpoint for
up to 180 seconds:

```
http profile=htb method=GET path=/api/v4/machine/active
```

Continue polling (with a few seconds between calls) until `ip` is
populated. If the IP is still null after 180 seconds, declare a timeout
and ask the user whether to terminate or keep waiting.

Once an IP is assigned, prove reachability through the VPN tunnel:

```
exec command="ping -c 3 <target_ip>" timeout=15
```

A successful reply confirms VPN + machine + routing all work. If pings
fail but the API says the box is up, suspect the VPN — re-check the
openvpn log via `process action=log` and surface any errors.

## Handoff

When everything is verified, summarize:

- `status`: `ready` | `failed`
- `machine`: `{ id, name, os, difficulty }`
- `target_ip`: e.g. `10.10.10.3`
- `vpn_config_path`: resolved `.ovpn` path (downloaded or override)
- `vpn_session_id`: openvpn `session_id` (the user can `process action=kill`
  it later)
- `cleanup`: the exact terminate call needed at the end

Hand this off to `htb-machine-solve` (or whatever exploitation work
follows) so the next skill does not redo any of this.

## Cleanup

When the user is done with the machine (form-encoded body, same as spawn):

```
http profile=htb method=POST path=/api/v4/vm/terminate data="machine_id=<machine_id>" headers={"Content-Type": "application/x-www-form-urlencoded"}
```

Then close the openvpn session:

```
process action=kill session_id=<vpn_session_id>
```

## Decision Rules

- If the user has not named a machine, ask before calling any API.
- If the machine is retired and `account_tier == free`, refuse — do not
  attempt to spawn. If `account_tier == vip`, retired is allowed.
- If another machine is already active, ask before terminating it
  (regardless of tier — the user may want it kept).
- For HTB POST endpoints (`/vm/spawn`, `/vm/terminate`, etc.), always send
  form-encoded bodies with `Content-Type: application/x-www-form-urlencoded`.
  JSON bodies are silently rejected.
- On 429 cooldown, surface the wait time and stop. Never auto-retry.
- On 403 with a Cloudflare body, surface and stop. Do not browser-fall
  back.
- If `ping` to the assigned IP fails, treat the VPN as the suspect first;
  do not proceed to exploitation work until reachability is proven.
- If `HTB_API_TOKEN` is unset or the `htb` profile is missing, stop
  immediately with the remediation message; do not guess endpoints.
- If multiple eligible VPN servers are returned, present the list and
  let the user choose; do not silently pick a region for them.
- On persistent TLS handshake errors during VPN up, delete the cached
  `.ovpn` and re-download once before surfacing failure to the user.

## References

- HTB profile settings (App Tokens): https://app.hackthebox.com/profile/settings
- HTB platform status: https://status.hackthebox.com
- HTB subscription tiers: https://help.hackthebox.com (search "subscription")
