"""Offline regression coverage for both independently bundled HTB helpers."""

import argparse
import importlib.util
import json
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
