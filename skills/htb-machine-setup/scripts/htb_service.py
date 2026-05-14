"""HackTheBox API helper — deterministic API calls for the htb-machine-setup
and htb-machine-solve skills.

Each subcommand performs one HTB API operation, prints structured JSON to
stdout on success, and a human-readable explanation to stderr (with a
non-zero exit code) on failure. The skill driver (the LLM) handles the
human-in-loop decisions: disambiguation, VPN server choice, openvpn
lifecycle, and error remediation.

Requires HTB_API_TOKEN in the environment. Kerkov already loads .env at
startup so subprocess inherits it.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

BASE_URL = "https://labs.hackthebox.com"

EXIT_AUTH = 2
EXIT_NOT_FOUND = 3
EXIT_AMBIGUOUS = 4
EXIT_COOLDOWN = 5
EXIT_BLOCKED = 6
EXIT_TIMEOUT = 7
EXIT_API = 8
EXIT_WRONG_FLAG = 9
EXIT_ALREADY_OWNED = 10


def _token() -> str:
    token = os.environ.get("HTB_API_TOKEN")
    if not token:
        print(
            "HTB_API_TOKEN is not set. Add it to .env and restart kerkov.",
            file=sys.stderr,
        )
        sys.exit(EXIT_AUTH)
    return token


def _request(
    method: str,
    path: str,
    *,
    form: Optional[dict[str, Any]] = None,
) -> tuple[int, dict[str, str], bytes]:
    url = f"{BASE_URL}{path}"
    headers = {
        "Authorization": f"Bearer {_token()}",
        "User-Agent": "kerkov-bot/0.1",
        "Accept": "application/json",
    }
    body: Optional[bytes] = None
    if form is not None:
        body = urllib.parse.urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _check_common_errors(
    status: int, headers: dict[str, str], body: bytes
) -> None:
    """Exit on the auth/rate/blocked failure modes shared by every endpoint."""
    if status == 401:
        print(
            "HTTP 401 from HTB — token invalid or expired. Regenerate at "
            "https://app.hackthebox.com/profile/settings",
            file=sys.stderr,
        )
        sys.exit(EXIT_AUTH)
    if status == 429:
        retry_after = headers.get("Retry-After", "")
        text = body.decode("utf-8", errors="replace")
        print(
            f"HTTP 429 cooldown. Retry-After: {retry_after}. Body: {text[:200]}",
            file=sys.stderr,
        )
        sys.exit(EXIT_COOLDOWN)
    if status == 403 and b"<html" in body[:512].lower():
        print(
            "HTTP 403 with Cloudflare HTML — HTB platform issue. "
            "Check https://status.hackthebox.com",
            file=sys.stderr,
        )
        sys.exit(EXIT_BLOCKED)


def _api_call(method: str, path: str, **kwargs) -> bytes:
    status, headers, body = _request(method, path, **kwargs)
    _check_common_errors(status, headers, body)
    if status >= 400:
        text = body.decode("utf-8", errors="replace")
        print(f"HTTP {status} from {path}: {text[:500]}", file=sys.stderr)
        sys.exit(EXIT_API)
    return body


def _json_call(method: str, path: str, **kwargs) -> Any:
    body = _api_call(method, path, **kwargs)
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        print(f"Non-JSON response from {path}: {body[:200]!r}", file=sys.stderr)
        sys.exit(EXIT_API)


def _unwrap_user(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        for key in ("info", "user", "data"):
            inner = payload.get(key)
            if isinstance(inner, dict):
                return inner
        return payload
    return {}


def cmd_whoami(_: argparse.Namespace) -> None:
    user = _unwrap_user(_json_call("GET", "/api/v4/user/info"))
    sub = user.get("subscription") if isinstance(user.get("subscription"), dict) else {}
    is_vip = bool(
        user.get("is_vip")
        or user.get("isVip")
        or sub.get("is_vip")
        or sub.get("vip")
    )
    print(
        json.dumps(
            {
                "account_tier": "vip" if is_vip else "free",
                "id": user.get("id"),
                "name": user.get("name") or user.get("username"),
            }
        )
    )


def cmd_find(args: argparse.Namespace) -> None:
    # HTB's /api/v4/search/fetch route was retired. The profile endpoint
    # accepts the machine name or id directly (case-insensitive), so we
    # look it up there and let 404 stand in for "not found".
    keyword = urllib.parse.quote(args.name, safe="")
    status, _, body = _request("GET", f"/api/v4/machine/profile/{keyword}")

    if status == 404:
        print(f"No HTB machine matched '{args.name}'.", file=sys.stderr)
        sys.exit(EXIT_NOT_FOUND)
    if status == 401:
        print(
            "HTTP 401 from HTB — token invalid or expired. Regenerate at "
            "https://app.hackthebox.com/profile/settings",
            file=sys.stderr,
        )
        sys.exit(EXIT_AUTH)
    if status >= 400:
        text = body.decode("utf-8", errors="replace")
        print(
            f"HTTP {status} from /api/v4/machine/profile/{keyword}: {text[:500]}",
            file=sys.stderr,
        )
        sys.exit(EXIT_API)

    try:
        profile = json.loads(body)
    except json.JSONDecodeError:
        print(
            f"Non-JSON response from /api/v4/machine/profile/{keyword}: "
            f"{body[:200]!r}",
            file=sys.stderr,
        )
        sys.exit(EXIT_API)

    info = _unwrap_user(profile) if isinstance(profile, dict) else {}
    if not info:
        info = profile if isinstance(profile, dict) else {}

    if not info.get("id"):
        print(f"No HTB machine matched '{args.name}'.", file=sys.stderr)
        sys.exit(EXIT_NOT_FOUND)

    print(
        json.dumps(
            {
                "id": info.get("id"),
                "name": info.get("name"),
                "os": info.get("os"),
                "difficulty": info.get("difficulty") or info.get("difficultyText"),
                "retired": bool(info.get("retired")),
                "ip": info.get("ip"),
            }
        )
    )


def _machine_profile(machine_id: str) -> dict[str, Any]:
    keyword = urllib.parse.quote(machine_id, safe="")
    status, headers, body = _request("GET", f"/api/v4/machine/profile/{keyword}")
    _check_common_errors(status, headers, body)
    if status == 404:
        print(f"No HTB machine matched '{machine_id}'.", file=sys.stderr)
        sys.exit(EXIT_NOT_FOUND)
    if status >= 400:
        text = body.decode("utf-8", errors="replace")
        print(
            f"HTTP {status} from /api/v4/machine/profile/{keyword}: {text[:500]}",
            file=sys.stderr,
        )
        sys.exit(EXIT_API)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        print(
            f"Non-JSON response from /api/v4/machine/profile/{keyword}: "
            f"{body[:200]!r}",
            file=sys.stderr,
        )
        sys.exit(EXIT_API)
    info = _unwrap_user(payload) if isinstance(payload, dict) else {}
    if not info:
        info = payload if isinstance(payload, dict) else {}
    return info


def cmd_status(args: argparse.Namespace) -> None:
    info = _machine_profile(args.machine_id)

    # HTB has shifted field names across API versions, so check all the
    # plausible spellings for the per-user ownership flags.
    user_owned = bool(
        info.get("authUserInUserOwns")
        or info.get("user_own")
        or info.get("userOwn")
        or info.get("isOwnedUser")
    )
    root_owned = bool(
        info.get("authUserInRootOwns")
        or info.get("root_own")
        or info.get("rootOwn")
        or info.get("isOwnedRoot")
    )

    print(
        json.dumps(
            {
                "machine_id": info.get("id"),
                "name": info.get("name"),
                "os": info.get("os"),
                "user_owned": user_owned,
                "root_owned": root_owned,
            }
        )
    )


def cmd_submit(args: argparse.Namespace) -> None:
    form: dict[str, Any] = {"id": args.machine_id, "flag": args.flag}
    if args.difficulty is not None:
        form["difficulty"] = args.difficulty

    status, headers, body = _request("POST", "/api/v4/machine/own", form=form)
    _check_common_errors(status, headers, body)

    text = body.decode("utf-8", errors="replace")
    try:
        data = json.loads(text) if text else {}
    except json.JSONDecodeError:
        data = {}
    msg = data.get("message", "") if isinstance(data, dict) else ""

    if status >= 400:
        lower = msg.lower()
        if "incorrect" in lower or "invalid" in lower or "wrong" in lower:
            print(f"Incorrect flag: {msg}", file=sys.stderr)
            sys.exit(EXIT_WRONG_FLAG)
        if "already" in lower:
            print(f"Already owned: {msg}", file=sys.stderr)
            sys.exit(EXIT_ALREADY_OWNED)
        print(
            f"HTTP {status} from /api/v4/machine/own: {text[:500]}",
            file=sys.stderr,
        )
        sys.exit(EXIT_API)

    print(
        json.dumps(
            {
                "submitted": True,
                "machine_id": args.machine_id,
                "message": msg or "ok",
            }
        )
    )


def cmd_active(_: argparse.Namespace) -> None:
    data = _json_call("GET", "/api/v4/machine/active")
    info = data.get("info") if isinstance(data, dict) else None
    if not isinstance(info, dict) or not info:
        print(json.dumps({"active": None}))
        return
    print(
        json.dumps(
            {
                "active": {
                    "id": info.get("id"),
                    "name": info.get("name"),
                    "ip": info.get("ip"),
                }
            }
        )
    )


def cmd_spawn(args: argparse.Namespace) -> None:
    _api_call("POST", "/api/v4/vm/spawn", form={"machine_id": args.machine_id})
    print(json.dumps({"spawned": True, "machine_id": args.machine_id}))


def cmd_terminate(args: argparse.Namespace) -> None:
    _api_call("POST", "/api/v4/vm/terminate", form={"machine_id": args.machine_id})
    print(json.dumps({"terminated": True, "machine_id": args.machine_id}))


def _normalize_server(s: dict[str, Any]) -> dict[str, Any]:
    name = str(s.get("friendly_name") or s.get("name") or "")
    tier = "vip" if "vip" in name.lower() else "free"
    return {
        "id": s.get("id"),
        "name": s.get("friendly_name") or s.get("name"),
        "region": s.get("location") or s.get("region"),
        "tier": tier,
        "current_clients": s.get("current_clients"),
        "full": bool(s.get("full")),
    }


def cmd_vpn_servers(args: argparse.Namespace) -> None:
    # HTB requires the lab product to be specified. "labs" covers machines.
    data = _json_call("GET", "/api/v4/connections/servers?product=labs")

    # Shape: {status, data: {disabled, assigned: <server>,
    # options: {<region>: {<group>: {servers: {<id>: <server>}}}}}}
    # The `assigned` server is the only one we can download an .ovpn for
    # without first POSTing a switch — surface it separately so the skill
    # driver can prefer it.
    payload = data.get("data") if isinstance(data, dict) else None
    if not isinstance(payload, dict):
        payload = {}

    assigned_raw = payload.get("assigned") if isinstance(payload.get("assigned"), dict) else None
    assigned = _normalize_server(assigned_raw) if assigned_raw else None
    assigned_id = assigned["id"] if assigned else None

    raw: list[dict[str, Any]] = []
    seen: set[Any] = set()

    def collect(node: Any) -> None:
        if isinstance(node, dict):
            if "friendly_name" in node and "id" in node:
                sid = node.get("id")
                if sid not in seen:
                    seen.add(sid)
                    raw.append(node)
                return
            for v in node.values():
                collect(v)
        elif isinstance(node, list):
            for item in node:
                collect(item)

    # Walk only the options tree so the assigned server isn't double-listed.
    collect(payload.get("options"))

    out = [_normalize_server(s) for s in raw if s.get("id") != assigned_id]

    if args.tier == "free":
        out = [s for s in out if s["tier"] == "free"]
    # tier=vip: leave both VIP and free in — VIP users can connect to either.

    print(json.dumps({"assigned": assigned, "servers": out}))


def cmd_vpn_switch(args: argparse.Namespace) -> None:
    _api_call("POST", f"/api/v4/connections/servers/switch/{args.server_id}")
    print(json.dumps({"switched": True, "server_id": args.server_id}))


def cmd_vpn_download(args: argparse.Namespace) -> None:
    body = _api_call("GET", f"/api/v4/access/ovpnfile/{args.server_id}/0")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(body)
    print(json.dumps({"path": str(output.resolve()), "bytes": len(body)}))


def cmd_wait_ip(args: argparse.Namespace) -> None:
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        data = _json_call("GET", "/api/v4/machine/active")
        info = data.get("info") if isinstance(data, dict) else None
        ip = info.get("ip") if isinstance(info, dict) else None
        if ip:
            print(
                json.dumps(
                    {
                        "ip": ip,
                        "machine_id": info.get("id"),
                        "name": info.get("name"),
                    }
                )
            )
            return
        time.sleep(args.interval)
    print(
        f"Timed out after {args.timeout}s waiting for an IP on the active machine.",
        file=sys.stderr,
    )
    sys.exit(EXIT_TIMEOUT)


def main() -> None:
    parser = argparse.ArgumentParser(prog="htb-service")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("whoami").set_defaults(fn=cmd_whoami)

    p_find = sub.add_parser("find")
    p_find.add_argument("name")
    p_find.set_defaults(fn=cmd_find)

    sub.add_parser("active").set_defaults(fn=cmd_active)

    p_status = sub.add_parser("status")
    p_status.add_argument("machine_id")
    p_status.set_defaults(fn=cmd_status)

    p_submit = sub.add_parser("submit")
    p_submit.add_argument("machine_id")
    p_submit.add_argument("flag")
    p_submit.add_argument("--difficulty", type=int, default=None)
    p_submit.set_defaults(fn=cmd_submit)

    p_spawn = sub.add_parser("spawn")
    p_spawn.add_argument("machine_id")
    p_spawn.set_defaults(fn=cmd_spawn)

    p_term = sub.add_parser("terminate")
    p_term.add_argument("machine_id")
    p_term.set_defaults(fn=cmd_terminate)

    p_servers = sub.add_parser("vpn-servers")
    p_servers.add_argument("--tier", choices=["free", "vip"])
    p_servers.set_defaults(fn=cmd_vpn_servers)

    p_switch = sub.add_parser("vpn-switch")
    p_switch.add_argument("server_id")
    p_switch.set_defaults(fn=cmd_vpn_switch)

    p_dl = sub.add_parser("vpn-download")
    p_dl.add_argument("server_id")
    p_dl.add_argument("--output", required=True)
    p_dl.set_defaults(fn=cmd_vpn_download)

    p_wait = sub.add_parser("wait-ip")
    p_wait.add_argument("--timeout", type=int, default=180)
    p_wait.add_argument("--interval", type=int, default=5)
    p_wait.set_defaults(fn=cmd_wait_ip)

    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
