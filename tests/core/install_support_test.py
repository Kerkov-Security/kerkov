"""User data and shell configuration survive repeated installs."""

import os
import subprocess
from unittest.mock import patch

from kerkov.core.install_support import (
    chromium_available,
    configure_path,
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


def test_preflight_timeout_is_nonfatal(capsys):
    with patch(
        "kerkov.core.install_support.subprocess.run",
        side_effect=subprocess.TimeoutExpired("probe", 5),
    ):
        assert not chromium_available()
        preflight()
    assert "credentials.json (0600)" in capsys.readouterr().out
