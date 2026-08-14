"""Tests for per-user interactive configuration."""

import os

import pytest

from kerkov.core import config_manager
from kerkov.core.config_manager import (
    USER_SETTINGS_FILE_ENV_VAR,
    ConfigManager,
    load_config,
    save_user_setting,
)


def test_execution_and_write_tool_are_disabled_by_default() -> None:
    config = ConfigManager()

    assert config.execution_enabled is False
    assert config.write_tool_enabled is False


def test_execution_can_be_enabled_without_enabling_write_tool() -> None:
    config = ConfigManager(EXECUTION_ENABLED=True)

    assert config.execution_enabled is True
    assert config.write_tool_enabled is False


def test_ollama_url_persists_in_user_settings(
    tmp_path,
    monkeypatch,
) -> None:
    settings_path = tmp_path / ".kerkov" / "settings.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(USER_SETTINGS_FILE_ENV_VAR, str(settings_path))
    monkeypatch.delenv("OLLAMA_BASE", raising=False)

    saved_path = save_user_setting(
        "OLLAMA_BASE",
        "https://ollama.example:11434",
    )
    config = load_config(env_file=str(tmp_path / "missing.env"))

    assert saved_path == settings_path
    assert config.ollama_base == "https://ollama.example:11434"
    if os.name != "nt":
        assert settings_path.stat().st_mode & 0o777 == 0o600
        assert settings_path.parent.stat().st_mode & 0o777 == 0o700


def test_environment_ollama_url_remains_an_operator_override(
    tmp_path,
    monkeypatch,
) -> None:
    settings_path = tmp_path / "settings.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(USER_SETTINGS_FILE_ENV_VAR, str(settings_path))
    save_user_setting("OLLAMA_BASE", "https://saved.example:11434")
    monkeypatch.setenv("OLLAMA_BASE", "https://environment.example:11434")

    config = load_config(env_file=str(tmp_path / "missing.env"))

    assert config.ollama_base == "https://environment.example:11434"


def test_saving_setting_preserves_existing_custom_parent_permissions(
    tmp_path,
    monkeypatch,
) -> None:
    settings_path = tmp_path / "shared" / "settings.json"
    settings_path.parent.mkdir()
    monkeypatch.setenv(USER_SETTINGS_FILE_ENV_VAR, str(settings_path))
    if os.name != "nt":
        os.chmod(settings_path.parent, 0o755)

    save_user_setting("OLLAMA_BASE", "https://ollama.example:11434")

    if os.name != "nt":
        assert settings_path.parent.stat().st_mode & 0o777 == 0o755


def test_saving_setting_hardens_existing_managed_parent_directory(
    tmp_path,
    monkeypatch,
) -> None:
    settings_path = tmp_path / ".kerkov" / "settings.json"
    settings_path.parent.mkdir()
    monkeypatch.setattr(
        config_manager,
        "DEFAULT_USER_SETTINGS_PATH",
        str(settings_path),
    )
    monkeypatch.delenv(USER_SETTINGS_FILE_ENV_VAR, raising=False)
    if os.name != "nt":
        os.chmod(settings_path.parent, 0o755)

    save_user_setting("OLLAMA_BASE", "https://ollama.example:11434")

    if os.name != "nt":
        assert settings_path.parent.stat().st_mode & 0o777 == 0o700


def test_only_supported_user_settings_can_be_saved(
    tmp_path,
    monkeypatch,
) -> None:
    settings_path = tmp_path / "settings.json"
    monkeypatch.setenv(USER_SETTINGS_FILE_ENV_VAR, str(settings_path))

    with pytest.raises(ValueError, match="Unsupported user setting"):
        save_user_setting("GOOGLE_API_KEY", "must-not-be-written-here")

    assert not settings_path.exists()
