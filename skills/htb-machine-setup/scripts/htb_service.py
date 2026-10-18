"""HackTheBox API helper — deterministic API calls for the htb-machine-setup
and htb-machine-solve skills.

Each subcommand performs one HTB API operation, prints structured JSON to
stdout on success, and a human-readable explanation to stderr (with a
non-zero exit code) on failure. The skill driver (the LLM) handles the
human-in-loop decisions: disambiguation, VPN server choice, openvpn
lifecycle, and error remediation.

Resolves HTB_API_TOKEN from the environment for legacy/non-interactive use,
then from Kerkov's persistent credential store.
"""

import argparse
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

BASE_URL = "https://labs.hackthebox.com"
MACHINE_OWN_PATH = "/api/v5/machine/own"

EXIT_AUTH = 2
EXIT_NOT_FOUND = 3
EXIT_COOLDOWN = 4
EXIT_BLOCKED = 5
EXIT_TIMEOUT = 6
EXIT_API = 7
EXIT_WRONG_FLAG = 8
EXIT_ALREADY_OWNED = 9


def _token() -> str:
    token = os.environ.get("HTB_API_TOKEN")
    if not token:
        try:
            from kerkov.core.credentials import (
                credential_spec_for_env_var,
                get_credential_manager,
            )

            token = get_credential_manager().get(
                credential_spec_for_env_var("HTB_API_TOKEN")
            )
        except Exception:
            token = None
    if not token:
        print(
            "HTB_API_TOKEN is not configured. Run `/auth set HTB_API_TOKEN` "
            "in Kerkov and retry.",
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
        "User-Agent": "kerkov/0.1",
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
    except (urllib.error.URLError, OSError) as e:
        print(
            f"HTB request to {path} failed: {e}. "
            "For a submission, check ownership before retrying.",
            file=sys.stderr,
        )
        sys.exit(EXIT_API)


def _check_common_errors(status: int, headers: dict[str, str], body: bytes) -> None:
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
    if not 200 <= status < 300:
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
        user.get("is_vip") or user.get("isVip") or sub.get("is_vip") or sub.get("vip")
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
    info = _machine_profile(args.name)

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


def _boolean(value: Any) -> Optional[bool]:
    """Decode API booleans without treating the string 'false' as owned."""
    if isinstance(value, str):
        value = value.strip().lower()
        if value in ("true", "1"):
            return True
        if value in ("false", "0"):
            return False
    elif isinstance(value, (bool, int)) and value in (0, 1):
        return bool(value)
    return None


def _ownership(info: dict[str, Any], *keys: str) -> Optional[bool]:
    for key in keys:
        if key in info:
            value = _boolean(info[key])
            if value is not None:
                return value
    return None


def cmd_status(args: argparse.Namespace) -> None:
    info = _machine_profile(args.machine_id)
    # Missing/unrecognized fields are unknown, not evidence of ownership.
    user_owned = _ownership(
        info, "authUserInUserOwns", "user_own", "userOwn", "isOwnedUser"
    )
    root_owned = _ownership(
        info, "authUserInRootOwns", "root_own", "rootOwn", "isOwnedRoot"
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
    flag = args.flag.strip()
    if re.fullmatch(r"[a-f0-9]{32}", flag) is None:
        print(
            "Machine flag must be exactly 32 lowercase hex characters.", file=sys.stderr
        )
        sys.exit(EXIT_WRONG_FLAG)
    form: dict[str, Any] = {"id": args.machine_id, "flag": flag}
    if args.difficulty is not None:
        form["difficulty"] = args.difficulty

    status, headers, body = _request("POST", MACHINE_OWN_PATH, form=form)
    _check_common_errors(status, headers, body)

    text = body.decode("utf-8", errors="replace")
    try:
        data = json.loads(text) if text else {}
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    msg = data.get("message")
    msg = msg if isinstance(msg, str) else ""
    lower = msg.lower()
    # HTB can return an application error with HTTP 200. Only flag-specific
    # errors get flag exit codes; an invalid difficulty/token is an API error.
    errors = data.get("errors")
    validation_error = bool(errors)
    if (
        not validation_error
        and status < 500
        and re.search(r"\balready (?:own(?:ed)?|submitted)\b", lower)
    ):
        print(f"Already owned: {msg}", file=sys.stderr)
        sys.exit(EXIT_ALREADY_OWNED)
    if (
        status < 500
        and not validation_error
        and re.search(
            r"\b(?:incorrect|invalid|wrong) flag\b|"
            r"\bflag (?:is )?(?:incorrect|invalid|wrong)\b",
            lower,
        )
    ):
        print(f"Incorrect flag: {msg}", file=sys.stderr)
        sys.exit(EXIT_WRONG_FLAG)

    success = _boolean(data.get("success"))
    result_status = data.get("status")
    explicit_failure = success is False or _boolean(result_status) is False
    if isinstance(result_status, str):
        explicit_failure = explicit_failure or result_status.lower() in (
            "error",
            "failed",
            "failure",
        )
    accepted = (
        success is True or _boolean(result_status) is True or result_status == "success"
    )
    if (
        not 200 <= status < 300
        or explicit_failure
        or validation_error
        or data.get("error")
        or not accepted
    ):
        print(
            f"Submission not confirmed. HTTP {status} from {MACHINE_OWN_PATH}: "
            f"{text[:500]}. Check ownership before retrying.",
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


def _confirmed_mutation(path: str, **kwargs) -> None:
    """Require application-level acceptance before reporting a lifecycle change."""
    data = _json_call("POST", path, **kwargs)
    if not isinstance(data, dict):
        data = {}
    success = _boolean(data.get("success"))
    status = data.get("status")
    rejected = success is False or _boolean(status) is False
    if isinstance(status, str):
        rejected = rejected or status.lower() in ("error", "failed", "failure")
    accepted = success is True or _boolean(status) is True or status == "success"
    if rejected or data.get("error") or data.get("errors") or not accepted:
        print(
            f"HTB operation not confirmed from {path}: "
            f"{json.dumps(data)[:500]}. Check current state before retrying.",
            file=sys.stderr,
        )
        sys.exit(EXIT_API)


def cmd_spawn(args: argparse.Namespace) -> None:
    _confirmed_mutation("/api/v4/vm/spawn", form={"machine_id": args.machine_id})
    print(json.dumps({"spawned": True, "machine_id": args.machine_id}))


def cmd_terminate(args: argparse.Namespace) -> None:
    _confirmed_mutation("/api/v4/vm/terminate", form={"machine_id": args.machine_id})
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

    assigned_raw = (
        payload.get("assigned") if isinstance(payload.get("assigned"), dict) else None
    )
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
    _confirmed_mutation(f"/api/v4/connections/servers/switch/{args.server_id}")
    print(json.dumps({"switched": True, "server_id": args.server_id}))


def cmd_vpn_download(args: argparse.Namespace) -> None:
    body = _api_call("GET", f"/api/v4/access/ovpnfile/{args.server_id}/0")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    # The config contains a private key. Stage it with owner-only permissions,
    # then replace atomically rather than following an existing output symlink.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(body)
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
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
    p_submit.add_argument(
        "--difficulty",
        type=int,
        choices=range(10, 101, 10),
        default=None,
        help="Optional rating in steps of 10 (10-100); supply if HTB requires it.",
    )
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
