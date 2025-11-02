"""
Configuration management for Kerkov Bot
"""

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

class ConfigManager(BaseModel):
    """Configuration manager for Kerkov Bot."""

    # LLM API Keys
    anthropic_api_key: Optional[str] = Field(None, alias="ANTHROPIC_API_KEY")
    openai_api_key: Optional[str] = Field(None, alias="OPENAI_API_KEY")
    google_api_key: Optional[str] = Field(None, alias="GOOGLE_API_KEY")

    # Default Settings
    default_model: str = Field("gemini-2.5-flash", alias="DEFAULT_MODEL")
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
    if env_file is None:
        current_dir = Path.cwd()
        # Check current directory and up to 3 parent directories
        for _ in range(4):
            env_path = current_dir / ".env"
            if env_path.exists():
                env_file = str(env_path)
                break
            current_dir = current_dir.parent

    if env_file and Path(env_file).exists():
        load_dotenv(env_file)

    config_dict = {
        key: os.getenv(key)
        for key in [
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "GOOGLE_API_KEY",
            "DEFAULT_MODEL",
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

    return ConfigManager(**config_dict)

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
