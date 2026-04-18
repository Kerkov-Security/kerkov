"""Tests for logger_factory module."""

import logging
import unittest
from unittest.mock import patch, MagicMock

import src.core.logging.logger_factory as factory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset_module_state():
    """Reset module-level globals so each test starts clean."""
    factory._log_file_path = None
    root = logging.getLogger()
    root.handlers = [
        h for h in root.handlers
        if not isinstance(h, logging.FileHandler)
        and not isinstance(h, MagicMock)
    ]


def _get_stream_handlers(logger: logging.Logger) -> list:
    """Return StreamHandlers on *logger* that are not FileHandlers."""
    return [
        h for h in logger.handlers
        if isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
    ]


def _cleanup_named_loggers(*names: str):
    """Remove handlers and reset level for named loggers."""
    for name in names:
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# _ensure_file_handler — one-time setup
# ---------------------------------------------------------------------------

class EnsureFileHandlerTest(unittest.TestCase):

    def setUp(self):
        _reset_module_state()

    def tearDown(self):
        _reset_module_state()

    @patch("src.core.logging.logger_factory.Path")
    @patch("logging.FileHandler")
    def test_creates_log_dir_on_first_call(self, mock_fh_cls, mock_path_cls):
        mock_path_cls.return_value = MagicMock()

        factory._ensure_file_handler()

        mock_path_cls.assert_called_once_with(factory.DEFAULT_LOGGING_DIR)
        mock_path_cls.return_value.mkdir.assert_called_once_with(exist_ok=True)

    @patch("src.core.logging.logger_factory.Path")
    @patch("logging.FileHandler")
    def test_creates_file_handler_on_first_call(self, mock_fh_cls, mock_path_cls):
        mock_path_cls.return_value = MagicMock()

        factory._ensure_file_handler()

        mock_fh_cls.assert_called_once()
        assert factory._log_file_path is not None

    @patch("src.core.logging.logger_factory.Path")
    @patch("logging.FileHandler")
    def test_sets_root_logger_to_info(self, mock_fh_cls, mock_path_cls):
        mock_path_cls.return_value = MagicMock()

        factory._ensure_file_handler()

        root = logging.getLogger()
        assert root.level == logging.INFO

    @patch("src.core.logging.logger_factory.Path")
    @patch("logging.FileHandler")
    def test_only_runs_once(self, mock_fh_cls, mock_path_cls):
        mock_path_cls.return_value = MagicMock()

        factory._ensure_file_handler()
        factory._ensure_file_handler()

        mock_fh_cls.assert_called_once()


# ---------------------------------------------------------------------------
# get_logger — named logger creation
# ---------------------------------------------------------------------------

class GetLoggerTest(unittest.TestCase):

    def setUp(self):
        _reset_module_state()

    def tearDown(self):
        _reset_module_state()
        _cleanup_named_loggers(
            "TestClass", "DebugLogger", "ConsoleLogger",
            "NoDuplicates", "LoggerA", "LoggerB",
        )

    # --- basic behaviour ---

    @patch("src.core.logging.logger_factory._ensure_file_handler")
    def test_calls_ensure_file_handler(self, mock_ensure):
        factory.get_logger("TestClass")

        mock_ensure.assert_called_once()

    @patch("src.core.logging.logger_factory._ensure_file_handler")
    def test_returns_named_logger(self, mock_ensure):
        logger = factory.get_logger("TestClass")

        assert logger.name == "TestClass"

    # --- log level ---

    @patch("src.core.logging.logger_factory._ensure_file_handler")
    def test_default_log_level_is_info(self, mock_ensure):
        logger = factory.get_logger("TestClass")

        assert logger.level == logging.INFO

    @patch("src.core.logging.logger_factory._ensure_file_handler")
    def test_sets_log_level_on_named_logger(self, mock_ensure):
        logger = factory.get_logger("DebugLogger", log_level=logging.DEBUG)

        assert logger.level == logging.DEBUG

    @patch("src.core.logging.logger_factory._ensure_file_handler")
    def test_different_loggers_get_independent_levels(self, mock_ensure):
        logger_a = factory.get_logger("LoggerA", log_level=logging.DEBUG)
        logger_b = factory.get_logger("LoggerB", log_level=logging.ERROR)

        assert logger_a.level == logging.DEBUG
        assert logger_b.level == logging.ERROR

    # --- console handler ---

    @patch("src.core.logging.logger_factory._ensure_file_handler")
    def test_no_console_handler_by_default(self, mock_ensure):
        logger = factory.get_logger("TestClass")

        assert _get_stream_handlers(logger) == []

    @patch("src.core.logging.logger_factory._ensure_file_handler")
    def test_adds_console_handler_when_requested(self, mock_ensure):
        logger = factory.get_logger("ConsoleLogger", also_print_to_console=True)

        assert len(_get_stream_handlers(logger)) == 1

    @patch("src.core.logging.logger_factory._ensure_file_handler")
    def test_does_not_duplicate_console_handler(self, mock_ensure):
        factory.get_logger("NoDuplicates", also_print_to_console=True)
        factory.get_logger("NoDuplicates", also_print_to_console=True)

        logger = logging.getLogger("NoDuplicates")
        assert len(_get_stream_handlers(logger)) == 1

    @patch("src.core.logging.logger_factory._ensure_file_handler")
    def test_different_loggers_get_independent_console_handlers(self, mock_ensure):
        logger_a = factory.get_logger("LoggerA", also_print_to_console=True)
        logger_b = factory.get_logger("LoggerB", also_print_to_console=False)

        assert len(_get_stream_handlers(logger_a)) == 1
        assert len(_get_stream_handlers(logger_b)) == 0


if __name__ == "__main__":
    unittest.main()
