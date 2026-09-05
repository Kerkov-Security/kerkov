"""Asset discovery must work without a source checkout or trusted cwd."""

from pathlib import Path
from unittest.mock import patch

import pytest

from kerkov.core.config_manager import ConfigManager
from kerkov.core.project_paths import resolve_project_dir


def test_checkout_assets_take_precedence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = Path(__file__).resolve().parents[2]
    assert resolve_project_dir("skills") == root / "skills"


def test_installed_assets_ignore_unrelated_project(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "target"
    project.mkdir()
    (project / "pyproject.toml").write_text('[project]\nname = "target"\n')
    (project / "skills").mkdir()
    monkeypatch.chdir(project)
    bundled = tmp_path / "site-packages" / "kerkov" / "bundled"
    with patch("kerkov.core.project_paths._find_project_root", return_value=None):
        with patch(
            "kerkov.core.project_paths.resources.files", return_value=bundled.parent
        ):
            assert resolve_project_dir("skills") == bundled / "skills"
            assert ConfigManager().http_profiles_path == str(
                bundled / "config/http_profiles.yaml"
            )
            user = home / ".kerkov" / "skills"
            user.mkdir(parents=True)
            assert resolve_project_dir("skills") == user
            # An existing user config directory must not hide a missing file.
            (home / ".kerkov" / "config").mkdir()
            assert (
                resolve_project_dir("config/http_profiles.yaml")
                == bundled / "config/http_profiles.yaml"
            )
            profile = home / ".kerkov" / "config/http_profiles.yaml"
            profile.touch()
            assert ConfigManager().http_profiles_path == str(profile)


def test_explicit_http_profile_path_is_preserved():
    assert (
        ConfigManager(HTTP_PROFILES_PATH="custom.yaml").http_profiles_path
        == "custom.yaml"
    )


@pytest.mark.parametrize("name", ["../outside", "/tmp/outside"])
def test_asset_paths_cannot_escape(name):
    with pytest.raises(ValueError):
        resolve_project_dir(name)
