import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

DEFAULT_LOGGING_DIR = "logs"
_LOG_TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

_log_file_path: Optional[str] = None


def _ensure_file_handler() -> None:
    """One-time setup: create the shared file handler on the root logger."""
    global _log_file_path
    if _log_file_path is not None:
        return

    logging_dir = os.environ.get("KERKOV_LOGGING_DIR", DEFAULT_LOGGING_DIR)
    Path(logging_dir).mkdir(parents=True, exist_ok=True)
    _log_file_path = f"{logging_dir}/kerkov_{_LOG_TIMESTAMP}.log"

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    file_handler = logging.FileHandler(_log_file_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    root.addHandler(file_handler)


def get_logger(
    class_name: str,
    log_level: int = logging.INFO,
    also_print_to_console: bool = False,
) -> logging.Logger:
    _ensure_file_handler()

    logger = logging.getLogger(class_name)
    logger.setLevel(log_level)

    if also_print_to_console and not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in logger.handlers
    ):
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        logger.addHandler(console_handler)

    return logger
