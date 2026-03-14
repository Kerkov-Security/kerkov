"""
Configuration management for Kerkov Bot
"""

import os
from pathlib import Path
from typing import Optional

from dotenv import dotenv_values, load_dotenv
from pydantic import BaseModel, Field, field_validator

from src.core.json_state import load_json_file, resolve_repo_path, write_json_file

DEFAULT_CODEX_CONFIG_PATH = "config/codex.json"
DEFAULT_CODEX_STATE_PATH = "data/providers/codex/auth.json"

class ConfigManager(BaseModel):
    """Configuration manager for Kerkov Bot."""

    # LLM API Keys
    anthropic_api_key: Optional[str] = Field(None, alias="ANTHROPIC_API_KEY")
    openai_api_key: Optional[str] = Field(None, alias="OPENAI_API_KEY")
    google_api_key: Optional[str] = Field(None, alias="GOOGLE_API_KEY")
    ollama_base: Optional[str] = Field("http://localhost:11434", alias="OLLAMA_BASE")
    codex_access_token: Optional[str] = Field(None, alias="CODEX_ACCESS_TOKEN")
    codex_oauth_client_id: Optional[str] = Field(None)
    codex_oauth_client_secret: Optional[str] = Field(None)
    codex_oauth_refresh_token: Optional[str] = Field(None)
    codex_oauth_token_url: Optional[str] = Field(None)
    codex_oauth_authorize_url: Optional[str] = Field(None)
    codex_oauth_redirect_uri: Optional[str] = Field("http://localhost:1455/auth/callback")
    codex_oauth_scope: Optional[str] = Field("openid profile email offline_access")
    codex_base_url: Optional[str] = Field("https://chatgpt.com/backend-api")
    codex_state_path: str = Field(DEFAULT_CODEX_STATE_PATH)
    codex_account_id: Optional[str] = Field(None)
    codex_token_expires_at: Optional[str] = Field(None)

    # Default Settings
    llm_provider: str = Field("gemini", alias="LLM_PROVIDER")
    default_anthropic_model: str = Field("claude-sonnet-4-5-20250929", alias="DEFAULT_ANTHROPIC_MODEL")
    default_gemini_model: str = Field("gemini-2.5-flash", alias="DEFAULT_GEMINI_MODEL")
    default_ollama_model: str = Field("gpt-oss", alias="DEFAULT_OLLAMA_MODEL")
    default_codex_model: str = Field("gpt-5-codex", alias="DEFAULT_CODEX_MODEL")
    max_context_tokens: int = Field(50000, alias="MAX_CONTEXT_TOKENS")
    max_timeout_seconds: int = Field(60, alias="MAX_TIMEOUT_SECONDS")

    # Security Settings
    require_sudo_approval: bool = Field(True, alias="REQUIRE_SUDO_APPROVAL")
    sandbox_mode: bool = Field(True, alias="SANDBOX_MODE")
    max_rate_limit: int = Field(10, alias="MAX_RATE_LIMIT")

    # Logging
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    audit_log_path: str = Field("data/logs/audit.log", alias="AUDIT_LOG_PATH")

    # Session Management
    session_dir: str = Field("data/sessions", alias="SESSION_DIR")
    auto_save_interval: int = Field(300, alias="AUTO_SAVE_INTERVAL")

    class Config:
        """Pydantic configuration."""
        populate_by_name = True
        case_sensitive = False
        frozen = True

    @field_validator("require_sudo_approval", "sandbox_mode", mode="before")
    @classmethod
    def parse_bool(cls, v):
        """Parse boolean values from environment strings."""
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.lower() in ("true", "1", "yes", "on")
        return bool(v)

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v):
        """Validate log level."""
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        v_upper = v.upper()
        if v_upper not in valid_levels:
            raise ValueError(f"Log level must be one of {valid_levels}")
        return v_upper

def load_config(env_file: Optional[str] = None) -> ConfigManager:
    """
    Load configuration from environment variables and .env file.

    Args:
        env_file: Path to .env file. If None, searches for .env in current and parent directories.

    Returns:
        ConfigManager object with loaded settings
    """
    env_path = _find_env_path(env_file)
    if env_path is not None:
        env_file = str(env_path)

    if env_file and Path(env_file).exists():
        load_dotenv(env_file, override=True)

    config_dict = {
        key: os.getenv(key)
        for key in [
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "GOOGLE_API_KEY",
            "OLLAMA_BASE",
            "LLM_PROVIDER",
            "DEFAULT_ANTHROPIC_MODEL",
            "DEFAULT_GEMINI_MODEL",
            "DEFAULT_OLLAMA_MODEL",
            "DEFAULT_CODEX_MODEL",
            "MAX_CONTEXT_TOKENS",
            "MAX_TIMEOUT_SECONDS",
            "REQUIRE_SUDO_APPROVAL",
            "SANDBOX_MODE",
            "MAX_RATE_LIMIT",
            "LOG_LEVEL",
            "AUDIT_LOG_PATH",
            "SESSION_DIR",
            "AUTO_SAVE_INTERVAL",
        ]
        if os.getenv(key) is not None
    }

    codex_static_config = _load_codex_static_config(env_path)
    config_dict.update(codex_static_config)

    codex_state_path = str(
        resolve_repo_path(
            str(config_dict.get("codex_state_path", DEFAULT_CODEX_STATE_PATH)),
            start_dir=Path(env_file).parent if env_file else Path.cwd(),
        )
    )
    config_dict["codex_state_path"] = codex_state_path

    codex_state = load_json_file(Path(codex_state_path))
    if not codex_state:
        legacy_state = _load_legacy_codex_state(env_path)
        if legacy_state:
            write_json_file(Path(codex_state_path), legacy_state)
            codex_state = legacy_state

    if codex_state:
        for env_key in (
            "CODEX_ACCESS_TOKEN",
            "CODEX_OAUTH_REFRESH_TOKEN",
            "CODEX_ACCOUNT_ID",
            "CODEX_TOKEN_EXPIRES_AT",
        ):
            value = codex_state.get(env_key)
            if value is not None:
                config_dict[_codex_runtime_field_name(env_key)] = str(value)

    return ConfigManager(**config_dict)


def _find_env_path(env_file: Optional[str]) -> Optional[Path]:
    if env_file and Path(env_file).exists():
        return Path(env_file)

    current_dir = Path.cwd()
    for _ in range(4):
        candidate = current_dir / ".env"
        if candidate.exists():
            return candidate
        current_dir = current_dir.parent
    return None


def _load_legacy_codex_state(env_path: Optional[Path]) -> dict[str, str]:
    if env_path is None or not env_path.exists():
        return {}

    env_values = dotenv_values(str(env_path))
    legacy_keys = (
        "CODEX_ACCESS_TOKEN",
        "CODEX_OAUTH_REFRESH_TOKEN",
        "CODEX_ACCOUNT_ID",
        "CODEX_TOKEN_EXPIRES_AT",
    )
    return {
        key: str(value)
        for key in legacy_keys
        if (value := env_values.get(key))
    }


def _load_codex_static_config(env_path: Optional[Path]) -> dict[str, str]:
    start_dir = env_path.parent if env_path else Path.cwd()
    config_path = resolve_repo_path(DEFAULT_CODEX_CONFIG_PATH, start_dir=start_dir)
    static_config = load_json_file(config_path)

    if not static_config:
        legacy_values = _load_legacy_codex_static_config(env_path)
        if legacy_values:
            write_json_file(config_path, legacy_values)
            static_config = legacy_values

    return {
        _codex_static_field_name(key): str(value)
        for key, value in static_config.items()
        if value is not None
    }


def _load_legacy_codex_static_config(env_path: Optional[Path]) -> dict[str, str]:
    if env_path is None or not env_path.exists():
        return {}

    env_values = dotenv_values(str(env_path))
    legacy_keys = (
        "CODEX_OAUTH_CLIENT_ID",
        "CODEX_OAUTH_CLIENT_SECRET",
        "CODEX_OAUTH_AUTHORIZE_URL",
        "CODEX_OAUTH_TOKEN_URL",
        "CODEX_OAUTH_REDIRECT_URI",
        "CODEX_OAUTH_SCOPE",
        "CODEX_BASE_URL",
        "CODEX_STATE_PATH",
    )
    return {
        key: str(value)
        for key in legacy_keys
        if (value := env_values.get(key))
    }


def _codex_runtime_field_name(env_key: str) -> str:
    return {
        "CODEX_ACCESS_TOKEN": "codex_access_token",
        "CODEX_OAUTH_REFRESH_TOKEN": "codex_oauth_refresh_token",
        "CODEX_ACCOUNT_ID": "codex_account_id",
        "CODEX_TOKEN_EXPIRES_AT": "codex_token_expires_at",
    }[env_key]


def _codex_static_field_name(env_key: str) -> str:
    return {
        "CODEX_OAUTH_CLIENT_ID": "codex_oauth_client_id",
        "CODEX_OAUTH_CLIENT_SECRET": "codex_oauth_client_secret",
        "CODEX_OAUTH_AUTHORIZE_URL": "codex_oauth_authorize_url",
        "CODEX_OAUTH_TOKEN_URL": "codex_oauth_token_url",
        "CODEX_OAUTH_REDIRECT_URI": "codex_oauth_redirect_uri",
        "CODEX_OAUTH_SCOPE": "codex_oauth_scope",
        "CODEX_BASE_URL": "codex_base_url",
        "CODEX_STATE_PATH": "codex_state_path",
    }[env_key]

# Global config instance
_config: Optional[ConfigManager] = None


def get_config(reload: bool = False) -> ConfigManager:
    """
    Get the global configuration instance.

    Args:
        reload: If True, reload configuration from environment

    Returns:
        Global ConfigManager instance
    """
    global _config
    if _config is None or reload:
        _config = load_config()
    return _config
