"""User data and shell configuration survive repeated installs."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from kerkov.core.install_support import (
    chromium_available,
    configure_path,
    main,
    path_with_entry,
    preflight,
    refresh_installed_defaults,
    seed_user_data,
)
from kerkov.core.session.approval_rules import ApprovalRulesEngine


@pytest.fixture
def bundled_upgrade(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    bundle = tmp_path / "bundle"
    helper = bundle / "skills/htb-machine-setup/scripts/htb_service.py"
    helper.parent.mkdir(parents=True)
    helper.write_text("old shipped helper")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(
        "kerkov.core.install_support.bundled_asset_path", lambda name: bundle / name
    )
    monkeypatch.setattr("kerkov.core.install_support._legacy_asset_hashes", lambda: {})
    return home, bundle, helper


def test_upgrade_updates_unchanged_defaults_and_runtime_resolution(
    bundled_upgrade, monkeypatch
):
    from kerkov.core.project_paths import resolve_project_dir

    home, bundle, helper = bundled_upgrade
    assert seed_user_data() == 2
    helper.write_text("new security fix")
    assert seed_user_data() == 1
    monkeypatch.setattr("kerkov.core.project_paths._find_project_root", lambda _: None)
    resolved = (
        resolve_project_dir("skills") / "htb-machine-setup/scripts/htb_service.py"
    )
    assert resolved.read_text() == "new security fix"
    assert seed_user_data() == 0
    if os.name != "nt":
        assert resolved.stat().st_mode & 0o777 == 0o600
        assert (home / ".kerkov/.bundled-assets.json").stat().st_mode & 0o777 == 0o600


def test_multiple_upgrades_preserve_customizations_and_credentials(bundled_upgrade):
    home, bundle, helper = bundled_upgrade
    seed_user_data()
    user_helper = home / ".kerkov/skills/htb-machine-setup/scripts/htb_service.py"
    user_helper.write_text("my custom implementation")
    credentials = home / ".kerkov/credentials.json"
    credentials.write_text("private data")
    for version in ("new version", "newer version"):
        helper.write_text(version)
        assert seed_user_data() == 0
        assert user_helper.read_text() == "my custom implementation"
        assert credentials.read_text() == "private data"


def test_reverted_old_version_is_an_override_after_manifest_exists(
    bundled_upgrade, monkeypatch
):
    home, bundle, helper = bundled_upgrade
    seed_user_data()
    helper.write_text("second shipped version")
    seed_user_data()
    user_helper = home / ".kerkov/skills/htb-machine-setup/scripts/htb_service.py"
    user_helper.write_text("old shipped helper")
    monkeypatch.setattr(
        "kerkov.core.install_support._legacy_asset_hashes",
        lambda: {
            "skills/htb-machine-setup/scripts/htb_service.py": [
                hashlib.sha256(user_helper.read_bytes()).hexdigest()
            ]
        },
    )
    helper.write_text("third shipped version")
    assert seed_user_data() == 0
    assert user_helper.read_text() == "old shipped helper"


def test_legacy_known_default_updates_but_unknown_file_is_preserved(
    bundled_upgrade, monkeypatch
):
    home, bundle, helper = bundled_upgrade
    user_helper = home / ".kerkov/skills/htb-machine-setup/scripts/htb_service.py"
    user_helper.parent.mkdir(parents=True)
    user_helper.write_text("old shipped helper")
    custom = user_helper.parent / "custom.py"
    custom.write_text("my custom tool")
    (helper.parent / "custom.py").write_text("bundled tool")
    monkeypatch.setattr(
        "kerkov.core.install_support._legacy_asset_hashes",
        lambda: {
            "skills/htb-machine-setup/scripts/htb_service.py": [
                hashlib.sha256(user_helper.read_bytes()).hexdigest()
            ]
        },
    )
    helper.write_text("new security fix")
    assert seed_user_data() == 2
    assert user_helper.read_text() == "new security fix"
    assert custom.read_text() == "my custom tool"


def test_corrupt_manifest_preserves_existing_differences(bundled_upgrade):
    home, bundle, helper = bundled_upgrade
    seed_user_data()
    (home / ".kerkov/.bundled-assets.json").write_text("broken JSON")
    helper.write_text("new security fix")
    assert seed_user_data() == 0
    assert (
        home / ".kerkov/skills/htb-machine-setup/scripts/htb_service.py"
    ).read_text() == "old shipped helper"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink creation")
@pytest.mark.parametrize("directory_link", [False, True])
def test_upgrade_never_follows_user_asset_symlinks(bundled_upgrade, directory_link):
    home, bundle, helper = bundled_upgrade
    seed_user_data()
    user_helper = home / ".kerkov/skills/htb-machine-setup/scripts/htb_service.py"
    outside = home / "external/htb_service.py"
    outside.parent.mkdir()
    outside.write_text("old shipped helper")
    if directory_link:
        user_helper.unlink()
        user_helper.parent.rmdir()
        user_helper.parent.symlink_to(outside.parent, target_is_directory=True)
    else:
        user_helper.unlink()
        user_helper.symlink_to(outside)
    helper.write_text("new security fix")
    assert seed_user_data() == 0
    assert outside.read_text() == "old shipped helper"
    assert (user_helper.parent if directory_link else user_helper).is_symlink()


def test_failed_asset_replacement_does_not_advance_history(bundled_upgrade):
    home, bundle, helper = bundled_upgrade
    seed_user_data()
    manifest = home / ".kerkov/.bundled-assets.json"
    before = manifest.read_bytes()
    helper.write_text("new security fix")
    with patch(
        "kerkov.core.install_support.os.replace", side_effect=OSError("disk full")
    ):
        with pytest.raises(OSError, match="disk full"):
            seed_user_data()
    assert manifest.read_bytes() == before
    directory = home / ".kerkov/skills/htb-machine-setup/scripts"
    assert [path.name for path in directory.iterdir()] == ["htb_service.py"]
    assert (directory / "htb_service.py").read_text() == "old shipped helper"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink creation")
def test_manifest_symlink_is_rejected_without_writing_assets(bundled_upgrade):
    home, bundle, helper = bundled_upgrade
    seed_user_data()
    manifest = home / ".kerkov/.bundled-assets.json"
    manifest.unlink()
    target = home / "unrelated.json"
    target.write_text('{"private": "data"}')
    manifest.symlink_to(target)
    helper.write_text("new security fix")
    with pytest.raises(PermissionError, match="symlink"):
        seed_user_data()
    assert target.read_text() == '{"private": "data"}'
    assert (
        home / ".kerkov/skills/htb-machine-setup/scripts/htb_service.py"
    ).read_text() == "old shipped helper"


def test_installed_startup_refreshes_seeded_defaults(bundled_upgrade, monkeypatch):
    home, bundle, helper = bundled_upgrade
    seed_user_data()
    helper.write_text("startup upgrade")
    package = home / "package"
    (package / "bundled/skills").mkdir(parents=True)
    monkeypatch.setattr(
        "kerkov.core.install_support.resources.files", lambda _: package
    )
    refresh_installed_defaults()
    assert (
        home / ".kerkov/skills/htb-machine-setup/scripts/htb_service.py"
    ).read_text() == "startup upgrade"


@pytest.mark.parametrize("customized", ["helper", "approvals", "symlink"])
def test_installed_startup_preserves_overrides_without_stdout(
    bundled_upgrade, monkeypatch, capsys, customized
):
    home, bundle, helper = bundled_upgrade
    seed_user_data()
    saved = home / ".kerkov/skills/htb-machine-setup/scripts/htb_service.py"
    if customized == "helper":
        saved.write_text("custom helper")
    elif customized == "approvals":
        saved = home / ".kerkov/approvals.yaml"
        saved.write_text("custom rules")
    else:
        if os.name == "nt":
            pytest.skip("POSIX symlink creation")
        saved.unlink()
        saved.symlink_to(helper)
    before = saved.read_bytes()
    package = home / "package"
    (package / "bundled/skills").mkdir(parents=True)
    monkeypatch.setattr(
        "kerkov.core.install_support.resources.files", lambda _: package
    )
    capsys.readouterr()
    for _ in range(2):
        refresh_installed_defaults()
        assert capsys.readouterr().out == ""
        assert saved.read_bytes() == before
    seed_user_data()
    assert "Preserved" in capsys.readouterr().out


def test_source_checkout_does_not_seed_user_assets():
    with patch("kerkov.core.install_support.seed_user_data") as seed:
        refresh_installed_defaults()
    seed.assert_not_called()


def test_seed_defaults_without_overwriting_user_data(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
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
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
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
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
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
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    directory = tmp_path / ".kerkov"
    directory.mkdir()
    approvals = directory / "approvals.yaml"
    original = 'rules:\n  - allow: "read({})"\n'
    approvals.write_text(original)
    seed_user_data()
    assert approvals.read_text() == original
    assert "override packaged defaults" in capsys.readouterr().out


@pytest.mark.skipif(os.name == "nt", reason="Windows sets the registry PATH")
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


@pytest.mark.skipif(os.name == "nt", reason="Windows sets the registry PATH")
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


def test_windows_path_entry_is_prepended_once_and_keeps_unexpanded_entries():
    current = r"%USERPROFILE%\.local\bin;C:\Tools"
    updated = path_with_entry(current, r"C:\Users\me\.kerkov\bin")
    assert updated == r"C:\Users\me\.kerkov\bin;" + current
    assert path_with_entry(updated, r"c:\users\ME\.kerkov\bin\\") is None
    assert path_with_entry("", r"C:\bin") == r"C:\bin"


def test_windows_path_entry_matches_an_expanded_variable(monkeypatch):
    monkeypatch.setenv("KERKOV_TEST_HOME", r"C:\Users\me")
    assert path_with_entry(r'"%KERKOV_TEST_HOME%\bin"', r"C:\Users\me\bin") is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows registry")
def test_windows_user_path_preserves_expandable_registry_value():
    import uuid
    import winreg

    from kerkov.core.install_support import _prepend_user_path

    key_path = rf"Software\KerkovTest\{uuid.uuid4().hex}"
    access = winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, access) as key:
            winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, r"%TEMP%\x")
        assert _prepend_user_path(r"C:\kerkov bin", key_path)
        assert not _prepend_user_path(r"C:\kerkov bin", key_path)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            value, kind = winreg.QueryValueEx(key, "Path")
        assert (value, kind) == (r"C:\kerkov bin;%TEMP%\x", winreg.REG_EXPAND_SZ)
    finally:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\KerkovTest")
        except OSError:
            pass  # Another run's key is still present.


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
    assert "credentials.json (" in capsys.readouterr().out
