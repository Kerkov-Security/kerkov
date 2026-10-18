"""Offline regression coverage for both independently bundled HTB helpers."""

import argparse
import importlib.util
import json
import os
import stat
import sys
import urllib.error
from pathlib import Path
from unittest.mock import Mock

import pytest

SKILLS = Path(__file__).resolve().parents[2] / "skills"
FLAG = "0123456789abcdef0123456789abcdef"


@pytest.fixture(params=["htb-machine-solve", "htb-machine-setup"])
def service(request, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "htb_service", SKILLS / request.param / "scripts" / "htb_service.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # No real account, credential store, or network access in these tests.
    monkeypatch.setattr(
        module, "_token", Mock(side_effect=AssertionError("real token"))
    )
    monkeypatch.setattr(module, "_request", Mock(side_effect=AssertionError("network")))
    return module


def submit_args(flag=FLAG, difficulty=None):
    return argparse.Namespace(machine_id="123", flag=flag, difficulty=difficulty)


@pytest.mark.parametrize("existing", [False, True])
def test_vpn_download_is_private_even_when_replacing_public_file(
    service, tmp_path, capsys, existing
):
    output = tmp_path / "vpn" / "server.ovpn"
    if existing:
        output.parent.mkdir()
        output.write_bytes(b"old config")
        output.chmod(0o644)
    body = b"test VPN config with private key"
    service._request.side_effect = None
    service._request.return_value = (200, {}, body)

    service.cmd_vpn_download(argparse.Namespace(server_id="254", output=str(output)))

    assert output.read_bytes() == body
    if os.name != "nt":
        assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert list(output.parent.iterdir()) == [output]
    assert json.loads(capsys.readouterr().out) == {
        "path": str(output.resolve()),
        "bytes": len(body),
    }


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink permissions")
def test_vpn_download_replaces_symlink_without_overwriting_its_target(
    service, tmp_path, capsys
):
    unrelated = tmp_path / "unrelated"
    unrelated.write_bytes(b"preserve me")
    output = tmp_path / "server.ovpn"
    output.symlink_to(unrelated)
    service._request.side_effect = None
    service._request.return_value = (200, {}, b"new VPN config")

    service.cmd_vpn_download(argparse.Namespace(server_id="254", output=str(output)))

    assert unrelated.read_bytes() == b"preserve me"
    assert not output.is_symlink()
    assert output.read_bytes() == b"new VPN config"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert "new VPN config" not in capsys.readouterr().out


@pytest.mark.parametrize("operation", ["spawn", "terminate", "vpn_switch"])
@pytest.mark.parametrize(
    "status,body",
    [
        (200, b'{"success":false,"message":"Rejected"}'),
        (200, b'{"success":"false","message":"Rejected"}'),
        (200, b'{"success":true,"status":"error"}'),
        (200, b'{"success":true,"errors":{"machine_id":["Unavailable"]}}'),
        (200, b'{"message":"A request is already pending"}'),
        (200, b"{}"),
        (200, b"[]"),
        (200, b""),
        (200, b"<html>Login</html>"),
        (302, b'{"success":true}'),
        (500, b'{"success":true}'),
    ],
)
def test_lifecycle_never_reports_unconfirmed_success(
    service, capsys, operation, status, body
):
    service._request.side_effect = None
    service._request.return_value = (status, {}, body)

    with pytest.raises(SystemExit) as exc:
        getattr(service, f"cmd_{operation}")(
            argparse.Namespace(machine_id="123", server_id="456")
        )

    assert exc.value.code == service.EXIT_API
    output = capsys.readouterr()
    assert not output.out
    assert output.err
    assert service._request.call_count == 1


@pytest.mark.parametrize(
    "operation,path,form,result",
    [
        ("spawn", "/api/v4/vm/spawn", {"machine_id": "123"}, "spawned"),
        ("terminate", "/api/v4/vm/terminate", {"machine_id": "123"}, "terminated"),
        ("vpn_switch", "/api/v4/connections/servers/switch/456", None, "switched"),
    ],
)
@pytest.mark.parametrize("body", [b'{"success":true}', b'{"status":1}'])
def test_lifecycle_requires_api_acceptance(
    service, capsys, operation, path, form, result, body
):
    service._request.side_effect = None
    service._request.return_value = (200, {}, body)

    getattr(service, f"cmd_{operation}")(
        argparse.Namespace(machine_id="123", server_id="456")
    )

    kwargs = {"form": form} if form else {}
    service._request.assert_called_once_with("POST", path, **kwargs)
    assert json.loads(capsys.readouterr().out)[result] is True


@pytest.mark.parametrize(
    "operation,message,result",
    [
        ("spawn", "Playing machine.", "spawned"),
        ("terminate", "Stopped playing machine.", "terminated"),
    ],
)
def test_lifecycle_accepts_endpoint_specific_message_acknowledgements(
    service, capsys, operation, message, result
):
    service._request.side_effect = None
    service._request.return_value = (200, {}, json.dumps({"message": message}).encode())
    getattr(service, f"cmd_{operation}")(argparse.Namespace(machine_id="123"))
    assert json.loads(capsys.readouterr().out)[result] is True


@pytest.mark.parametrize("operation", ["spawn", "terminate", "vpn_switch"])
@pytest.mark.parametrize(
    "body",
    [
        {"message": "Playing machine.", "success": False},
        {"message": "Stopped playing machine.", "status": "error"},
        {"message": "Playing machine.", "errors": ["Unavailable"]},
        {"message": "Machine not found."},
    ],
)
def test_positive_message_never_overrides_rejection(service, capsys, operation, body):
    service._request.side_effect = None
    service._request.return_value = (200, {}, json.dumps(body).encode())
    with pytest.raises(SystemExit) as exc:
        getattr(service, f"cmd_{operation}")(
            argparse.Namespace(machine_id="123", server_id="456")
        )
    assert exc.value.code == service.EXIT_API
    assert not capsys.readouterr().out


@pytest.mark.parametrize(
    "operation,message",
    [
        ("spawn", "Stopped playing machine."),
        ("terminate", "Playing machine."),
        ("vpn_switch", "Playing machine."),
    ],
)
def test_lifecycle_message_must_match_endpoint(service, operation, message):
    service._request.side_effect = None
    service._request.return_value = (200, {}, json.dumps({"message": message}).encode())
    with pytest.raises(SystemExit) as exc:
        getattr(service, f"cmd_{operation}")(
            argparse.Namespace(machine_id="123", server_id="456")
        )
    assert exc.value.code == service.EXIT_API


@pytest.mark.parametrize("difficulty", [None, 10, 50, 100])
def test_submit_uses_v5_and_preserves_optional_rating(service, capsys, difficulty):
    service._request.return_value = (200, {}, b'{"success":true,"message":"Owned"}')
    service._request.side_effect = None

    service.cmd_submit(submit_args(f" {FLAG}\r\n", difficulty))

    form = {"id": "123", "flag": FLAG}
    if difficulty is not None:
        form["difficulty"] = difficulty
    service._request.assert_called_once_with("POST", "/api/v5/machine/own", form=form)
    result = json.loads(capsys.readouterr().out)
    assert result == {"submitted": True, "machine_id": "123", "message": "Owned"}


@pytest.mark.parametrize(
    "status,body,exit_code",
    [
        (200, b'{"success":false,"message":"Incorrect flag"}', 8),
        (400, b'{"message":"The flag is invalid"}', 8),
        (200, b'{"message":"You already own this machine"}', 9),
        (200, b'{"message":"User already owned"}', 9),
        (400, b'{"message":"Flag already submitted"}', 9),
        (200, b'{"success":false,"message":"Rejected"}', 7),
        (200, b'{"success":"false","message":"Rejected"}', 7),
        (200, b'{"success":true,"status":false}', 7),
        (200, b'{"success":true,"status":"error"}', 7),
        (200, b'{"success":true,"errors":{"difficulty":["Invalid rating"]}}', 7),
        (
            422,
            b'{"message":"Invalid difficulty","errors":{"difficulty":["Required"]}}',
            7,
        ),
        (400, b'{"message":"Invalid token"}', 7),
        (409, b'{"message":"A request is already pending"}', 7),
        (200, b"{}", 7),
        (200, b"[]", 7),
        (200, b"", 7),
        (200, b"<html>Login</html>", 7),
        (302, b'{"success":true}', 7),
        (500, b'{"success":true}', 7),
        (500, b'{"message":"Already owned"}', 7),
        (401, b'{"message":"Invalid flag"}', 2),
        (429, b'{"message":"Already owned"}', 4),
        (403, b"<html>Cloudflare</html>", 5),
    ],
)
def test_submit_never_reports_false_success(service, capsys, status, body, exit_code):
    service._request.side_effect = None
    service._request.return_value = (status, {"Retry-After": "60"}, body)

    with pytest.raises(SystemExit) as exc:
        service.cmd_submit(submit_args())

    assert exc.value.code == exit_code
    output = capsys.readouterr()
    assert not output.out
    assert output.err
    assert service._request.call_count == 1


@pytest.mark.parametrize(
    "flag", ["password", FLAG[:-1], FLAG + "a", FLAG[:16] + "\n" + FLAG[16:]]
)
def test_invalid_flag_is_rejected_before_network(service, flag):
    with pytest.raises(SystemExit) as exc:
        service.cmd_submit(submit_args(flag))

    assert exc.value.code == service.EXIT_WRONG_FLAG
    service._request.assert_not_called()


@pytest.mark.parametrize(
    "value,expected",
    [
        (True, True),
        (False, False),
        (1, True),
        (0, False),
        ("true", True),
        ("false", False),
        ("1", True),
        ("0", False),
        (None, None),
        ("unknown", None),
    ],
)
def test_status_decodes_ownership_without_truthiness(
    service, monkeypatch, capsys, value, expected
):
    monkeypatch.setattr(
        service,
        "_machine_profile",
        lambda _: {
            "id": 123,
            "authUserInUserOwns": value,
            "authUserInRootOwns": "false",
        },
    )

    service.cmd_status(argparse.Namespace(machine_id="123"))

    result = json.loads(capsys.readouterr().out)
    assert result["machine_id"] == 123
    assert result["user_owned"] is expected
    assert result["root_owned"] is False


def test_missing_ownership_fields_are_unknown(service, monkeypatch, capsys):
    monkeypatch.setattr(service, "_machine_profile", lambda _: {"id": 123})
    service.cmd_status(argparse.Namespace(machine_id="123"))
    result = json.loads(capsys.readouterr().out)
    assert result["user_owned"] is None
    assert result["root_owned"] is None


@pytest.mark.parametrize("rating", ["1", "5", "11", "101"])
def test_cli_rejects_old_or_invalid_rating_scale(service, monkeypatch, rating):
    monkeypatch.setattr(
        sys, "argv", ["htb-service", "submit", "123", FLAG, "--difficulty", rating]
    )
    with pytest.raises(SystemExit) as exc:
        service.main()
    assert exc.value.code == 2
    service._request.assert_not_called()


def test_network_timeout_reports_uncertain_submission_without_retry(
    service, monkeypatch, capsys
):
    # Exercise the real transport with a fake token and mocked urlopen.
    monkeypatch.undo()
    monkeypatch.setattr(service, "_token", lambda: "test-token")
    request = Mock(side_effect=urllib.error.URLError("timed out"))
    monkeypatch.setattr(service.urllib.request, "urlopen", request)
    with pytest.raises(SystemExit) as exc:
        service.cmd_submit(submit_args())
    assert exc.value.code == service.EXIT_API
    assert "check ownership before retrying" in capsys.readouterr().err
    assert request.call_count == 1


def test_bundled_htb_helpers_stay_in_sync():
    root = Path(__file__).resolve().parents[2] / "skills"
    assert (root / "htb-machine-setup/scripts/htb_service.py").read_bytes() == (
        root / "htb-machine-solve/scripts/htb_service.py"
    ).read_bytes()
