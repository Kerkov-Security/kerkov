"""User data and shell configuration survive repeated installs."""

import os
import subprocess
import sys
from unittest.mock import patch

import pytest

from kerkov.cli.approval_rules import ApprovalRulesEngine
from kerkov.core.install_support import (
    chromium_available,
    configure_path,
    main,
    preflight,
    seed_user_data,
)


def test_seed_defaults_without_overwriting_user_data(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert seed_user_data() > 0
    home = tmp_path / ".kerkov"
    skills = home / "skills"
    assert (skills / "htb-machine-solve/scripts/htb_service.py").is_file()
    assert (home / "agents/example-recon/AGENT.md").is_file()
    assert (home / "config/http_profiles.yaml").is_file()
    approvals = home / "approvals.yaml"
    approvals.write_text("rules: []\n")
    credentials = home / "credentials.json"
    credentials.write_text("private data")
    assert seed_user_data() == 0
    assert approvals.read_text() == "rules: []\n"
    assert credentials.read_text() == "private data"
    if os.name != "nt":
        assert approvals.stat().st_mode & 0o777 == 0o600


def test_seeded_approval_template_inherits_updated_packaged_rules(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    seed_user_data()
    approvals = tmp_path / ".kerkov/approvals.yaml"
    packaged = tmp_path / "packaged-approvals.yaml"
    packaged.write_text('rules:\n  - allow: "read({})"\n')
    params = {"file_path": "/home/test/.ssh/id_rsa"}
    assert (
        ApprovalRulesEngine.from_files(approvals, packaged).evaluate("read", params)
        == "allow"
    )
    packaged.write_text(
        "rules:\n  - deny: \"read({'file_path': '*/.ssh/*'})\"\n"
        '  - allow: "read({})"\n'
    )
    assert (
        ApprovalRulesEngine.from_files(approvals, packaged).evaluate("read", params)
        == "deny"
    )


def test_seed_reports_preserved_asset_differences(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    seed_user_data()
    capsys.readouterr()
    skill = tmp_path / ".kerkov/skills/localhost-scan/SKILL.md"
    skill.write_text("my custom skill")
    assert seed_user_data() == 0
    output = capsys.readouterr().out
    assert str(skill) in output
    assert "differs from" in output
    assert "bundled default" in output
    assert "my custom skill" not in output
    assert skill.read_text() == "my custom skill"


def test_seed_preserves_existing_approval_overrides_with_notice(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("HOME", str(tmp_path))
    directory = tmp_path / ".kerkov"
    directory.mkdir()
    approvals = directory / "approvals.yaml"
    original = 'rules:\n  - allow: "read({})"\n'
    approvals.write_text(original)
    seed_user_data()
    assert approvals.read_text() == original
    assert "override packaged defaults" in capsys.readouterr().out


def test_shell_setup_is_idempotent_and_quotes_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ZDOTDIR", str(tmp_path / "zsh"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    github_path = tmp_path / "github-path"
    monkeypatch.setenv("GITHUB_PATH", str(github_path))
    bin_dir = tmp_path / "tools with 'quotes' and $dollars" / "bin"
    profile = tmp_path / ".bash_profile"
    profile.write_text("# existing customization\n")
    configure_path(bin_dir)
    original = profile.read_text()
    configure_path(bin_dir)
    assert profile.read_text() == original
    assert original.startswith("# existing customization\n")
    result = subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-c",
            '. "$1"; printf "%s" "$PATH"',
            "bash",
            str(profile),
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.startswith(str(bin_dir) + ":")
    assert (tmp_path / "zsh/.zshrc").is_file()
    assert (tmp_path / "config/fish/conf.d/kerkov.fish").is_file()
    assert github_path.read_text().splitlines() == [str(bin_dir)] * 2


def test_shell_setup_preserves_non_utf8_profiles(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ZDOTDIR", str(tmp_path / "zsh"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("GITHUB_PATH", raising=False)
    profile = tmp_path / "zsh/.zshrc"
    profile.parent.mkdir()
    original = b"# caf\xe9\n"
    profile.write_bytes(original)
    configure_path(tmp_path / "bin")
    updated = profile.read_bytes()
    assert updated.startswith(original)
    assert b"# Kerkov installer" in updated
    configure_path(tmp_path / "bin")
    assert profile.read_bytes() == updated
    assert (tmp_path / ".bashrc").is_file()


@pytest.mark.parametrize("option", ["--seed", "--path"])
def test_setup_failure_has_remediation_without_traceback(
    tmp_path, monkeypatch, capsys, option
):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "install_support",
            option,
            *([str(tmp_path / "bin")] if option == "--path" else []),
        ],
    )
    failing_function = "seed_user_data" if option == "--seed" else "configure_path"
    with patch(
        f"kerkov.core.install_support.{failing_function}",
        side_effect=PermissionError("not writable"),
    ):
        assert main() == 1
    stderr = capsys.readouterr().err
    assert "not writable" in stderr
    assert "permissions" in stderr
    assert option in stderr
    assert "Traceback" not in stderr


def test_preflight_timeout_is_nonfatal(capsys):
    with patch(
        "kerkov.core.install_support.subprocess.run",
        side_effect=subprocess.TimeoutExpired("probe", 5),
    ):
        assert not chromium_available()
        preflight()
    assert "credentials.json (0600)" in capsys.readouterr().out
