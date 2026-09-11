"""Tests for per-user interactive configuration."""

import os
import unittest
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from kerkov.core import config_manager
from kerkov.core.config_manager import (
    USER_SETTINGS_FILE_ENV_VAR,
    ConfigManager,
    load_config,
    remove_user_settings,
    save_user_setting,
)


class ReadUrlConfigTest(unittest.TestCase):
    def test_read_url_defaults(self) -> None:
        config = ConfigManager()

        self.assertEqual(config.read_max_chars, 20000)
        self.assertEqual(config.read_timeout_seconds, 15)

    def test_read_url_limits_must_be_positive(self) -> None:
        for setting in ("READ_MAX_CHARS", "READ_TIMEOUT_SECONDS"):
            for value in (0, -1):
                with self.subTest(setting=setting, value=value):
                    with self.assertRaises(ValidationError):
                        ConfigManager(**{setting: value})

    def test_read_url_settings_are_loaded_from_environment(self) -> None:
        with (
            patch.dict(
                os.environ,
                {"READ_MAX_CHARS": "4567", "READ_TIMEOUT_SECONDS": "8"},
                clear=True,
            ),
            patch.object(config_manager, "_find_env_path", return_value=None),
            patch.object(config_manager, "_load_user_settings", return_value={}),
            patch.object(config_manager, "_load_codex_static_config", return_value={}),
            patch.object(config_manager, "load_json_file", return_value={}),
        ):
            config = load_config(env_file="missing.env")

        self.assertEqual(config.read_max_chars, 4567)
        self.assertEqual(config.read_timeout_seconds, 8)


def test_execution_and_write_tool_are_enabled_by_default() -> None:
    config = ConfigManager()

    assert config.execution_enabled is True
    assert config.write_tool_enabled is True


def test_execution_can_be_disabled_without_disabling_write_tool() -> None:
    config = ConfigManager(EXECUTION_ENABLED=False)

    assert config.execution_enabled is False
    assert config.write_tool_enabled is True


def test_tool_output_normalization_defaults() -> None:
    config = ConfigManager()

    assert config.tool_output_max_chars == 6000
    assert config.tool_output_head_chars == 1500
    assert config.tool_output_tail_chars == 500


def test_tool_output_normalization_accepts_aliases_and_oversized_preview() -> None:
    config = ConfigManager(
        TOOL_OUTPUT_MAX_CHARS=100,
        TOOL_OUTPUT_HEAD_CHARS=80,
        TOOL_OUTPUT_TAIL_CHARS=40,
    )

    assert config.tool_output_max_chars == 100
    assert config.tool_output_head_chars == 80
    assert config.tool_output_tail_chars == 40


@pytest.mark.parametrize("head,tail", [(0, 10), (10, 0)])
def test_tool_output_normalization_accepts_one_sided_preview(
    head: int, tail: int
) -> None:
    config = ConfigManager(
        tool_output_head_chars=head,
        tool_output_tail_chars=tail,
    )

    assert config.tool_output_head_chars == head
    assert config.tool_output_tail_chars == tail


@pytest.mark.parametrize(
    "settings,match",
    [
        ({"TOOL_OUTPUT_MAX_CHARS": 0}, "TOOL_OUTPUT_MAX_CHARS"),
        ({"TOOL_OUTPUT_MAX_CHARS": -1}, "TOOL_OUTPUT_MAX_CHARS"),
        ({"TOOL_OUTPUT_HEAD_CHARS": -1}, "TOOL_OUTPUT_HEAD_CHARS"),
        ({"TOOL_OUTPUT_TAIL_CHARS": -1}, "TOOL_OUTPUT_TAIL_CHARS"),
        (
            {"TOOL_OUTPUT_HEAD_CHARS": 0, "TOOL_OUTPUT_TAIL_CHARS": 0},
            "must total at least one character",
        ),
    ],
)
def test_tool_output_normalization_rejects_invalid_settings(
    settings: dict[str, int], match: str
) -> None:
    with pytest.raises(ValidationError, match=match):
        ConfigManager(**settings)


def test_tool_output_normalization_settings_load_from_environment(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(USER_SETTINGS_FILE_ENV_VAR, str(tmp_path / "settings.json"))
    monkeypatch.setenv("TOOL_OUTPUT_MAX_CHARS", "1200")
    monkeypatch.setenv("TOOL_OUTPUT_HEAD_CHARS", "700")
    monkeypatch.setenv("TOOL_OUTPUT_TAIL_CHARS", "300")

    config = load_config(env_file=str(tmp_path / "missing.env"))

    assert config.tool_output_max_chars == 1200
    assert config.tool_output_head_chars == 700
    assert config.tool_output_tail_chars == 300


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


def test_zap_url_persists_in_user_settings(tmp_path, monkeypatch) -> None:
    settings_path = tmp_path / ".kerkov" / "settings.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(USER_SETTINGS_FILE_ENV_VAR, str(settings_path))
    monkeypatch.delenv("ZAP_API_URL", raising=False)

    saved_path = save_user_setting("ZAP_API_URL", "http://127.0.0.1:8093")
    config = load_config(env_file=str(tmp_path / "missing.env"))

    assert saved_path == settings_path
    assert config.zap_api_url == "http://127.0.0.1:8093"


def test_managed_zap_marker_persists_in_user_settings(tmp_path, monkeypatch) -> None:
    settings_path = tmp_path / ".kerkov" / "settings.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(USER_SETTINGS_FILE_ENV_VAR, str(settings_path))
    monkeypatch.delenv("ZAP_MANAGED", raising=False)

    save_user_setting("ZAP_MANAGED", "true")
    config = load_config(env_file=str(tmp_path / "missing.env"))

    assert config.zap_managed is True


def test_managed_zap_settings_can_be_removed_without_affecting_others(
    tmp_path,
    monkeypatch,
) -> None:
    settings_path = tmp_path / ".kerkov" / "settings.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(USER_SETTINGS_FILE_ENV_VAR, str(settings_path))
    monkeypatch.delenv("OLLAMA_BASE", raising=False)
    monkeypatch.delenv("ZAP_API_URL", raising=False)
    monkeypatch.delenv("ZAP_MANAGED", raising=False)
    save_user_setting("OLLAMA_BASE", "https://ollama.example:11434")
    save_user_setting("ZAP_API_URL", "http://127.0.0.1:8093")
    save_user_setting("ZAP_MANAGED", "true")

    removed_path = remove_user_settings("ZAP_API_URL", "ZAP_MANAGED")
    config = load_config(env_file=str(tmp_path / "missing.env"))

    assert removed_path == settings_path
    assert config.ollama_base == "https://ollama.example:11434"
    assert config.zap_api_url == "http://127.0.0.1:8090"
    assert config.zap_managed is False


def test_environment_zap_url_overrides_user_setting(tmp_path, monkeypatch) -> None:
    settings_path = tmp_path / "settings.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(USER_SETTINGS_FILE_ENV_VAR, str(settings_path))
    save_user_setting("ZAP_API_URL", "http://127.0.0.1:8093")
    monkeypatch.setenv("ZAP_API_URL", "http://127.0.0.1:8094")

    config = load_config(env_file=str(tmp_path / "missing.env"))

    assert config.zap_api_url == "http://127.0.0.1:8094"


def test_codex_static_environment_overrides_are_loaded(tmp_path, monkeypatch) -> None:
    state_path = tmp_path / "state" / "codex-auth.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODEX_STATE_PATH", str(state_path))
    monkeypatch.setenv("CODEX_OAUTH_CALLBACK_BIND_HOST", "0.0.0.0")

    config = load_config(env_file=str(tmp_path / "missing.env"))

    assert config.codex_state_path == str(state_path)
    assert config.codex_oauth_callback_bind_host == "0.0.0.0"


def test_zap_runtime_defaults_are_loopback_and_official_image() -> None:
    config = ConfigManager()

    assert config.zap_api_url == "http://127.0.0.1:8090"
    assert config.zap_api_allow_remote is False
    assert config.zap_managed is False
    assert config.zap_docker_image == "ghcr.io/zaproxy/zaproxy:stable"
    assert config.zap_docker_container_name == "kerkov-zap"


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
