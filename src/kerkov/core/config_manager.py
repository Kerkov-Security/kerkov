"""
Configuration management for Kerkov Bot
"""

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

from kerkov.core.json_state import load_json_file, resolve_repo_path, write_json_file

DEFAULT_CODEX_CONFIG_PATH = "config/codex.json"
DEFAULT_CODEX_STATE_PATH = "data/providers/codex/auth.json"
DEFAULT_USER_SETTINGS_PATH = "~/.kerkov/settings.json"
USER_SETTINGS_FILE_ENV_VAR = "KERKOV_SETTINGS_FILE"
SUPPORTED_USER_SETTINGS = frozenset({"OLLAMA_BASE"})


class ConfigManager(BaseModel):
    """Configuration manager for Kerkov Bot."""

    # Legacy/non-interactive credential fallbacks. Interactive clients resolve
    # these through CredentialManager and the OS keyring first.
    anthropic_api_key: Optional[str] = Field(None, alias="ANTHROPIC_API_KEY")
    openai_api_key: Optional[str] = Field(None, alias="OPENAI_API_KEY")
    google_api_key: Optional[str] = Field(None, alias="GOOGLE_API_KEY")
    nvidia_api_key: Optional[str] = Field(None, alias="NVIDIA_API_KEY")
    nvidia_base_url: str = Field(
        "https://integrate.api.nvidia.com/v1", alias="NVIDIA_BASE_URL"
    )
    openrouter_api_key: Optional[str] = Field(None, alias="OPENROUTER_API_KEY")
    openrouter_base_url: str = Field(
        "https://openrouter.ai/api/v1", alias="OPENROUTER_BASE_URL"
    )
    openrouter_http_referer: Optional[str] = Field(
        None, alias="OPENROUTER_HTTP_REFERER"
    )
    openrouter_app_title: Optional[str] = Field(None, alias="OPENROUTER_APP_TITLE")
    ollama_base: Optional[str] = Field("http://localhost:11434", alias="OLLAMA_BASE")
    codex_access_token: Optional[str] = Field(None)
    codex_oauth_client_id: Optional[str] = Field(None)
    codex_oauth_client_secret: Optional[str] = Field(None)
    codex_oauth_refresh_token: Optional[str] = Field(None)
    codex_oauth_token_url: Optional[str] = Field(None)
    codex_oauth_authorize_url: Optional[str] = Field(None)
    codex_oauth_redirect_uri: Optional[str] = Field(
        "http://localhost:1455/auth/callback"
    )
    codex_oauth_scope: Optional[str] = Field("openid profile email offline_access")
    codex_base_url: Optional[str] = Field("https://chatgpt.com/backend-api")
    codex_state_path: str = Field(DEFAULT_CODEX_STATE_PATH)
    codex_account_id: Optional[str] = Field(None)
    codex_token_expires_at: Optional[str] = Field(None)

    # Default Settings
    llm_provider: str = Field("gemini", alias="LLM_PROVIDER")
    default_anthropic_model: str = Field(
        "claude-sonnet-4-5-20250929", alias="DEFAULT_ANTHROPIC_MODEL"
    )
    default_gemini_model: str = Field("gemini-2.5-flash", alias="DEFAULT_GEMINI_MODEL")
    default_ollama_model: str = Field("gpt-oss", alias="DEFAULT_OLLAMA_MODEL")
    default_nvidia_model: str = Field("z-ai/glm-5.2", alias="DEFAULT_NVIDIA_MODEL")
    default_openrouter_model: str = Field(
        "openrouter/auto", alias="DEFAULT_OPENROUTER_MODEL"
    )
    default_codex_model: str = Field("gpt-5.4", alias="DEFAULT_CODEX_MODEL")
    max_context_tokens: int = Field(50000, alias="MAX_CONTEXT_TOKENS")
    max_response_tokens: Optional[int] = Field(None, alias="MAX_RESPONSE_TOKENS")
    compaction_threshold_pct: float = Field(0.65, alias="COMPACTION_THRESHOLD_PCT")
    summary_reserve_tokens: int = Field(2000, alias="SUMMARY_RESERVE_TOKENS")
    max_timeout_seconds: int = Field(60, alias="MAX_TIMEOUT_SECONDS")
    http_profiles_path: str = Field(
        "config/http_profiles.yaml", alias="HTTP_PROFILES_PATH"
    )
    agent_temperature: Optional[float] = Field(None, alias="AGENT_TEMPERATURE")

    # Web Search (WebSearchTool). The key may come from the runtime credential
    # manager; this field remains a non-interactive environment fallback.
    search_provider: str = Field("tavily", alias="SEARCH_PROVIDER")
    search_api_key: Optional[str] = Field(None, alias="SEARCH_API_KEY")
    search_max_results: int = Field(10, alias="SEARCH_MAX_RESULTS", gt=0)
    search_timeout_seconds: int = Field(15, alias="SEARCH_TIMEOUT_SECONDS", gt=0)

    # Agent subsystem caps
    agent_max_depth: int = Field(2, alias="AGENT_MAX_DEPTH", ge=0)
    agent_max_total: int = Field(20, alias="AGENT_MAX_TOTAL", ge=0)
    agent_max_batch: int = Field(5, alias="AGENT_MAX_BATCH", gt=0)
    agent_max_iterations: int = Field(30, alias="AGENT_MAX_ITERATIONS", gt=0)

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
    workspace_dir: str = Field("~/.kerkov/workspaces", alias="WORKSPACE_DIR")
    user_approvals_path: str = Field(
        "~/.kerkov/approvals.yaml", alias="USER_APPROVALS_PATH"
    )

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
            raise ValueError(f"VERBOSITY must be one of {sorted(valid)}, got {v!r}")
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
            raise ValueError(f"APPROVAL_MODE must be one of {sorted(valid)}, got {v!r}")
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
            raise ValueError(
                f"AGENT_TEMPERATURE must be between 0.0 and 2.0, got {temp}"
            )
        return temp

    @field_validator(
        "require_sudo_approval", "sandbox_mode", "allow_autonomous", mode="before"
    )
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
    from kerkov.core.logging.logger_factory import get_logger

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

    environment_config = {
        key: os.getenv(key)
        for key in [
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "GOOGLE_API_KEY",
            "NVIDIA_API_KEY",
            "NVIDIA_BASE_URL",
            "OPENROUTER_API_KEY",
            "OPENROUTER_BASE_URL",
            "OPENROUTER_HTTP_REFERER",
            "OPENROUTER_APP_TITLE",
            "OLLAMA_BASE",
            "LLM_PROVIDER",
            "DEFAULT_ANTHROPIC_MODEL",
            "DEFAULT_GEMINI_MODEL",
            "DEFAULT_OLLAMA_MODEL",
            "DEFAULT_NVIDIA_MODEL",
            "DEFAULT_OPENROUTER_MODEL",
            "DEFAULT_CODEX_MODEL",
            "MAX_CONTEXT_TOKENS",
            "MAX_RESPONSE_TOKENS",
            "COMPACTION_THRESHOLD_PCT",
            "SUMMARY_RESERVE_TOKENS",
            "MAX_TIMEOUT_SECONDS",
            "HTTP_PROFILES_PATH",
            "SEARCH_PROVIDER",
            "SEARCH_API_KEY",
            "SEARCH_MAX_RESULTS",
            "SEARCH_TIMEOUT_SECONDS",
            "AGENT_TEMPERATURE",
            "AGENT_MAX_DEPTH",
            "AGENT_MAX_TOTAL",
            "AGENT_MAX_BATCH",
            "AGENT_MAX_ITERATIONS",
            "REQUIRE_SUDO_APPROVAL",
            "SANDBOX_MODE",
            "MAX_RATE_LIMIT",
            "AUTONOMY_LEVEL",
            "ALLOW_AUTONOMOUS",
            "VERBOSITY",
            "APPROVAL_MODE",
            "LOG_LEVEL",
            "AUDIT_LOG_PATH",
            "WORKSPACE_DIR",
            "USER_APPROVALS_PATH",
            "SESSION_DIR",
            "AUTO_SAVE_INTERVAL",
        ]
        if os.getenv(key) is not None
    }
    config_dict = _load_user_settings()
    config_dict.update(environment_config)

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


def get_user_settings_path() -> Path:
    """Return the per-user settings file used by interactive configuration."""

    configured = os.environ.get(
        USER_SETTINGS_FILE_ENV_VAR,
        DEFAULT_USER_SETTINGS_PATH,
    )
    return Path(configured).expanduser()


def save_user_setting(name: str, value: str) -> Path:
    """Persist a supported non-secret setting outside the repository."""

    if name not in SUPPORTED_USER_SETTINGS:
        raise ValueError(f"Unsupported user setting: {name}")

    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} cannot be empty.")

    path = get_user_settings_path()
    if path.is_symlink():
        raise PermissionError("User settings file must not be a symlink.")
    if path.parent.exists() and path.parent.is_symlink():
        raise PermissionError("User settings directory must not be a symlink.")

    settings = load_json_file(path)
    settings[name] = normalized
    parent_existed = path.parent.exists()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt" and not parent_existed:
        os.chmod(path.parent, 0o700)
    write_json_file(path, settings)
    if os.name != "nt":
        os.chmod(path, 0o600)
    return path


def _load_user_settings() -> dict[str, str]:
    path = get_user_settings_path()
    if path.is_symlink():
        return {}
    data = load_json_file(path)
    return {
        key: str(value)
        for key, value in data.items()
        if key in SUPPORTED_USER_SETTINGS and value is not None
    }


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
