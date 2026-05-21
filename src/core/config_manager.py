"""
Configuration management for Kerkov Bot
"""

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

from src.core.json_state import load_json_file, resolve_repo_path

DEFAULT_CODEX_CONFIG_PATH = "config/codex.json"
DEFAULT_CODEX_STATE_PATH = "data/providers/codex/auth.json"

class ConfigManager(BaseModel):
    """Configuration manager for Kerkov Bot."""

    # LLM API Keys
    anthropic_api_key: Optional[str] = Field(None, alias="ANTHROPIC_API_KEY")
    openai_api_key: Optional[str] = Field(None, alias="OPENAI_API_KEY")
    google_api_key: Optional[str] = Field(None, alias="GOOGLE_API_KEY")
    ollama_base: Optional[str] = Field("http://localhost:11434", alias="OLLAMA_BASE")
    codex_access_token: Optional[str] = Field(None)
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
    max_response_tokens: Optional[int] = Field(None, alias="MAX_RESPONSE_TOKENS")
    compaction_threshold_pct: float = Field(0.65, alias="COMPACTION_THRESHOLD_PCT")
    summary_reserve_tokens: int = Field(2000, alias="SUMMARY_RESERVE_TOKENS")
    max_timeout_seconds: int = Field(60, alias="MAX_TIMEOUT_SECONDS")
    http_profiles_path: str = Field("config/http_profiles.yaml", alias="HTTP_PROFILES_PATH")
    agent_temperature: Optional[float] = Field(None, alias="AGENT_TEMPERATURE")

    # Security Settings
    require_sudo_approval: bool = Field(True, alias="REQUIRE_SUDO_APPROVAL")
    sandbox_mode: bool = Field(True, alias="SANDBOX_MODE")
    max_rate_limit: int = Field(10, alias="MAX_RATE_LIMIT")

    # Autonomy Settings (legacy preset bundle — also fills verbosity + approval_mode when those are unset)
    autonomy_level: int = Field(2, alias="AUTONOMY_LEVEL")
    allow_autonomous: bool = Field(False, alias="ALLOW_AUTONOMOUS")

    # Explicit verbosity / approval axes (preferred). When unset, derive from AUTONOMY_LEVEL.
    verbosity: Optional[str] = Field(None, alias="VERBOSITY")
    approval_mode: Optional[str] = Field(None, alias="APPROVAL_MODE")

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

    @field_validator("autonomy_level", mode="before")
    @classmethod
    def validate_autonomy_level(cls, v):
        """Validate autonomy level is 1-4."""
        try:
            level = int(v)
        except (TypeError, ValueError):
            raise ValueError(f"AUTONOMY_LEVEL must be an integer 1-4, got {v!r}")
        if level < 1 or level > 4:
            raise ValueError(f"AUTONOMY_LEVEL must be between 1 and 4, got {level}")
        return level

    @field_validator("verbosity", mode="before")
    @classmethod
    def validate_verbosity(cls, v):
        """Validate verbosity name; empty/None means 'derive from AUTONOMY_LEVEL'."""
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return None
        valid = {"guided", "standard", "aggressive", "silent"}
        normalized = str(v).strip().lower()
        if normalized not in valid:
            raise ValueError(
                f"VERBOSITY must be one of {sorted(valid)}, got {v!r}"
            )
        return normalized

    @field_validator("approval_mode", mode="before")
    @classmethod
    def validate_approval_mode(cls, v):
        """Validate approval mode name; empty/None means 'derive from AUTONOMY_LEVEL'."""
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return None
        valid = {"strict", "normal", "permissive", "yolo"}
        normalized = str(v).strip().lower()
        if normalized not in valid:
            raise ValueError(
                f"APPROVAL_MODE must be one of {sorted(valid)}, got {v!r}"
            )
        return normalized

    @field_validator("max_response_tokens", mode="before")
    @classmethod
    def validate_max_response_tokens(cls, v):
        """Parse optional max response tokens; empty/None means 'use model cap'."""
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return None
        try:
            tokens = int(v)
        except (TypeError, ValueError):
            raise ValueError(f"MAX_RESPONSE_TOKENS must be an integer, got {v!r}")
        if tokens <= 0:
            raise ValueError(f"MAX_RESPONSE_TOKENS must be positive, got {tokens}")
        return tokens

    @field_validator("agent_temperature", mode="before")
    @classmethod
    def validate_agent_temperature(cls, v):
        """Parse and bounds-check optional agent temperature."""
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return None
        try:
            temp = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"AGENT_TEMPERATURE must be a number, got {v!r}")
        if temp < 0.0 or temp > 2.0:
            raise ValueError(f"AGENT_TEMPERATURE must be between 0.0 and 2.0, got {temp}")
        return temp

    @field_validator("require_sudo_approval", "sandbox_mode", "allow_autonomous", mode="before")
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
    from src.core.logging.logger_factory import get_logger

    logger = get_logger("ConfigManager")

    if env_file is None:
        current_dir = Path.cwd()
        # Check current directory and up to 3 parent directories
        for _ in range(4):
            env_path = current_dir / ".env"
            if env_path.exists():
                env_file = str(env_path)
                break
            current_dir = current_dir.parent
    env_path = _find_env_path(env_file)
    if env_path is not None:
        env_file = str(env_path)

    if env_file and Path(env_file).exists():
        logger.info(f"Loading .env from: {env_file}")
        load_dotenv(env_file)
    else:
        logger.warning("No .env file found, using defaults and environment variables")

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
            "MAX_RESPONSE_TOKENS",
            "COMPACTION_THRESHOLD_PCT",
            "SUMMARY_RESERVE_TOKENS",
            "MAX_TIMEOUT_SECONDS",
            "HTTP_PROFILES_PATH",
            "AGENT_TEMPERATURE",
            "REQUIRE_SUDO_APPROVAL",
            "SANDBOX_MODE",
            "MAX_RATE_LIMIT",
            "AUTONOMY_LEVEL",
            "ALLOW_AUTONOMOUS",
            "VERBOSITY",
            "APPROVAL_MODE",
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
                
    config = ConfigManager(**config_dict)
    logger.info(
        f"Config loaded: provider={config.llm_provider}, "
        f"log_level={config.log_level}, sandbox={config.sandbox_mode}, "
        f"max_tokens={config.max_context_tokens}"
    )
    return config


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


def _load_codex_static_config(env_path: Optional[Path]) -> dict[str, str]:
    start_dir = env_path.parent if env_path else Path.cwd()
    config_path = resolve_repo_path(DEFAULT_CODEX_CONFIG_PATH, start_dir=start_dir)
    static_config = load_json_file(config_path)

    return {
        _codex_static_field_name(key): str(value)
        for key, value in static_config.items()
        if value is not None
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
