import logging
from pathlib import Path
from unittest.mock import patch

import pytest

import kerkov.core.logging.logger_factory as factory


@pytest.fixture
def fresh_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Let one test perform the one-time file setup, then undo it."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    monkeypatch.setattr(factory, "_log_file_path", None)
    monkeypatch.setenv("KERKOV_LOGGING_DIR", str(tmp_path / "logs"))
    yield tmp_path / "logs"
    for handler in root.handlers:
        if handler not in handlers:
            handler.close()
    root.handlers, root.level = handlers, level


def _console_handlers(logger: logging.Logger) -> list[logging.Handler]:
    return [
        h
        for h in logger.handlers
        if isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
    ]


def test_first_logger_attaches_one_file_handler_to_root(fresh_factory: Path) -> None:
    root = logging.getLogger()
    before = len(root.handlers)

    factory.get_logger("logger_factory_test.a").info("first line")
    factory.get_logger("logger_factory_test.b")

    added = root.handlers[before:]
    assert len(added) == 1 and isinstance(added[0], logging.FileHandler)
    added[0].flush()
    log_files = list(fresh_factory.glob("kerkov_*.log"))
    assert len(log_files) == 1
    assert "first line" in log_files[0].read_text(encoding="utf-8")


@pytest.mark.parametrize("requested", [False, True])
def test_console_output_is_opt_in_and_never_duplicated(requested: bool) -> None:
    name = f"logger_factory_test.console.{requested}"
    try:
        with patch.object(factory, "_ensure_file_handler"):
            for _ in range(2):
                logger = factory.get_logger(name, also_print_to_console=requested)

        assert len(_console_handlers(logger)) == (1 if requested else 0)
    finally:
        logging.getLogger(name).handlers.clear()
