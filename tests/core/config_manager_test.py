"""Tests for per-user interactive configuration."""

import json
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
    load_user_setting,
    remove_user_settings,
    save_user_setting,
    save_user_settings,
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


def test_agent_routing_defaults_to_disabled() -> None:
    config = ConfigManager()

    assert config.agent_routing_enabled is False
    assert config.agent_routing_small_model == ""
    assert config.agent_routing_medium_model == ""
    assert config.agent_routing_large_model == ""


def test_agent_routing_settings_load_from_environment(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(USER_SETTINGS_FILE_ENV_VAR, str(tmp_path / "settings.json"))
    monkeypatch.setenv("AGENT_ROUTING_ENABLED", "true")
    monkeypatch.setenv("AGENT_ROUTING_SMALL_MODEL", "small")
    monkeypatch.setenv("AGENT_ROUTING_MEDIUM_MODEL", "medium")
    monkeypatch.setenv("AGENT_ROUTING_LARGE_MODEL", "large")

    config = load_config(env_file=str(tmp_path / "missing.env"))

    assert config.agent_routing_enabled is True
    assert config.agent_routing_small_model == "small"
    assert config.agent_routing_medium_model == "medium"
    assert config.agent_routing_large_model == "large"


def test_model_pricing_file_defaults_to_unset() -> None:
    assert ConfigManager().model_pricing_file is None


def test_model_pricing_file_loads_from_environment(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(USER_SETTINGS_FILE_ENV_VAR, str(tmp_path / "settings.json"))
    monkeypatch.setenv("MODEL_PRICING_FILE", "~/custom model prices.json")

    config = load_config(env_file=str(tmp_path / "missing.env"))

    assert config.model_pricing_file == "~/custom model prices.json"


def test_execution_can_be_disabled_without_disabling_write_tool() -> None:
    config = ConfigManager(EXECUTION_ENABLED=False)

    assert config.execution_enabled is False
    assert config.write_tool_enabled is True


def test_stream_output_is_enabled_by_default() -> None:
    config = ConfigManager()

    assert config.stream_output is True


@pytest.mark.parametrize("value", ["false", "0"])
def test_stream_output_can_be_disabled_from_environment(
    value: str,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(USER_SETTINGS_FILE_ENV_VAR, str(tmp_path / "settings.json"))
    monkeypatch.setenv("STREAM_OUTPUT", value)

    config = load_config(env_file=str(tmp_path / "missing.env"))

    assert config.stream_output is False


@pytest.mark.parametrize("value", ["false", "0"])
def test_stream_output_can_be_disabled_from_env_file(
    value: str,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(USER_SETTINGS_FILE_ENV_VAR, str(tmp_path / "settings.json"))
    monkeypatch.delenv("STREAM_OUTPUT", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(f"STREAM_OUTPUT={value}\n", encoding="utf-8")

    config = load_config(env_file=str(env_file))

    assert config.stream_output is False


def test_stream_output_stays_enabled_when_unset(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(USER_SETTINGS_FILE_ENV_VAR, str(tmp_path / "settings.json"))
    monkeypatch.delenv("STREAM_OUTPUT", raising=False)

    config = load_config(env_file=str(tmp_path / "missing.env"))

    assert config.stream_output is True


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


def test_deepseek_environment_configuration_is_loaded(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(USER_SETTINGS_FILE_ENV_VAR, str(tmp_path / "settings.json"))
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://deepseek.example/v1")
    monkeypatch.setenv("DEFAULT_DEEPSEEK_MODEL", "deepseek-v4-pro")

    config = load_config(env_file=str(tmp_path / "missing.env"))

    assert config.llm_provider == "deepseek"
    assert config.deepseek_api_key == "deepseek-test-key"
    assert config.deepseek_base_url == "https://deepseek.example/v1"
    assert config.default_deepseek_model == "deepseek-v4-pro"


def test_load_user_setting_round_trips_interface(tmp_path, monkeypatch) -> None:
    settings_path = tmp_path / "settings.json"
    monkeypatch.setenv(USER_SETTINGS_FILE_ENV_VAR, str(settings_path))

    assert load_user_setting("INTERFACE") is None
    save_user_setting("INTERFACE", "web")
    assert load_user_setting("INTERFACE") == "web"


def test_load_user_setting_ignores_unsupported_keys(tmp_path, monkeypatch) -> None:
    settings_path = tmp_path / "settings.json"
    monkeypatch.setenv(USER_SETTINGS_FILE_ENV_VAR, str(settings_path))

    assert load_user_setting("NOT_A_SETTING") is None


def test_provider_choice_persists_without_an_env_file(tmp_path, monkeypatch) -> None:
    settings_path = tmp_path / ".kerkov" / "settings.json"
    monkeypatch.chdir(tmp_path)

    with patch.dict(
        os.environ,
        {USER_SETTINGS_FILE_ENV_VAR: str(settings_path)},
        clear=True,
    ):
        initial_config = load_config()
        saved_path = save_user_setting("LLM_PROVIDER", "codex")
        reopened_config = load_config()

    assert initial_config.llm_provider == "gemini"
    assert saved_path == settings_path
    assert reopened_config.llm_provider == "codex"
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize("override_source", ["environment", "env_file"])
def test_explicit_provider_configuration_overrides_saved_choice(
    override_source: str,
    tmp_path,
    monkeypatch,
) -> None:
    settings_path = tmp_path / "settings.json"
    monkeypatch.chdir(tmp_path)

    with patch.dict(
        os.environ,
        {USER_SETTINGS_FILE_ENV_VAR: str(settings_path)},
        clear=True,
    ):
        save_user_setting("LLM_PROVIDER", "codex")
        if override_source == "environment":
            os.environ["LLM_PROVIDER"] = "ollama"
        else:
            (tmp_path / ".env").write_text("LLM_PROVIDER=ollama\n", encoding="utf-8")

        config = load_config()

    assert config.llm_provider == "ollama"


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


def test_provider_preferences_are_written_as_one_atomic_batch(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setenv(USER_SETTINGS_FILE_ENV_VAR, str(path))
    save_user_setting("OLLAMA_BASE", "http://localhost:11435")
    replacements = []
    original = os.replace

    def replace(source, target):
        replacements.append(json.loads(source.read_text()))
        original(source, target)

    monkeypatch.setattr(config_manager.os, "replace", replace)
    save_user_settings(
        {"LLM_PROVIDER": "gemini", "DEFAULT_GEMINI_MODEL": "chosen-model"}
    )
    assert replacements == [
        {
            "OLLAMA_BASE": "http://localhost:11435",
            "LLM_PROVIDER": "gemini",
            "DEFAULT_GEMINI_MODEL": "chosen-model",
        }
    ]
    assert json.loads(path.read_text()) == replacements[0]
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_failed_atomic_settings_write_preserves_entire_original_file(
    tmp_path, monkeypatch
):
    path = tmp_path / "settings.json"
    monkeypatch.setenv(USER_SETTINGS_FILE_ENV_VAR, str(path))
    save_user_settings({"LLM_PROVIDER": "gemini", "DEFAULT_GEMINI_MODEL": "old-model"})
    before = path.read_bytes()
    monkeypatch.setattr(
        config_manager.os,
        "replace",
        lambda *args: (_ for _ in ()).throw(OSError("disk failed")),
    )
    with pytest.raises(OSError):
        save_user_settings(
            {"LLM_PROVIDER": "ollama", "DEFAULT_OLLAMA_MODEL": "new-model"}
        )
    assert path.read_bytes() == before
    assert list(tmp_path.glob(".settings-*")) == []


@pytest.mark.parametrize("contents", ["", "not-json", "[]"])
def test_settings_save_preserves_malformed_existing_file(
    tmp_path, monkeypatch, contents
):
    path = tmp_path / "settings.json"
    path.write_text(contents)
    monkeypatch.setenv(USER_SETTINGS_FILE_ENV_VAR, str(path))
    with pytest.raises(ValueError, match=str(path)):
        save_user_settings({"LLM_PROVIDER": "gemini"})
    with pytest.raises(ValueError, match=str(path)):
        remove_user_settings("LLM_PROVIDER")
    assert path.read_text() == contents


def test_settings_batch_rejects_secret_field_before_any_write(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setenv(USER_SETTINGS_FILE_ENV_VAR, str(path))
    with pytest.raises(ValueError, match="Unsupported user setting"):
        save_user_settings({"LLM_PROVIDER": "gemini", "GOOGLE_API_KEY": "secret"})
    assert not path.exists()


@pytest.mark.parametrize("contents", ["", "private-invalid-json", "[]"])
def test_loading_malformed_settings_warns_with_path(
    tmp_path, monkeypatch, caplog, contents
):
    path = tmp_path / "settings.json"
    path.write_text(contents)
    monkeypatch.setenv(USER_SETTINGS_FILE_ENV_VAR, str(path))
    assert config_manager._load_user_settings() == {}
    assert str(path) in caplog.text
    assert "ignored" in caplog.text
    assert "private-invalid-json" not in caplog.text
    assert path.read_text() == contents
