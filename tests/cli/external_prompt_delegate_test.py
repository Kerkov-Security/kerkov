"""Tests for ExternalPromptDelegate."""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.cli.external_prompt_delegate import (
    ALLOWED_EXTENSIONS,
    MAX_PROMPT_BYTES,
    ExternalPromptDelegate,
    _resolve_editor,
    _strip_comments,
)


def _run(coro):
    return asyncio.run(coro)


class StripCommentsTest(unittest.TestCase):

    def test_strips_lines_starting_with_hash(self):
        text = "# header\nhello\n# trailing\nworld"
        self.assertEqual(_strip_comments(text), "hello\nworld")

    def test_strips_indented_hash_lines(self):
        text = "  # indented comment\nbody"
        self.assertEqual(_strip_comments(text), "body")

    def test_returns_empty_when_only_comments(self):
        text = "# one\n# two\n"
        self.assertEqual(_strip_comments(text), "")

    def test_strips_surrounding_whitespace(self):
        text = "\n\n  body line  \n\n"
        self.assertEqual(_strip_comments(text), "body line")


class ResolveEditorTest(unittest.TestCase):

    def test_visual_takes_precedence(self):
        with patch.dict(os.environ, {"VISUAL": "code -w", "EDITOR": "vim"}, clear=False):
            self.assertEqual(_resolve_editor()[:2], ["code", "-w"])

    def test_editor_used_when_visual_missing(self):
        env = {k: v for k, v in os.environ.items() if k not in {"VISUAL", "EDITOR"}}
        env["EDITOR"] = "nano"
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(_resolve_editor(), ["nano"])

    def test_falls_back_to_vim_when_unset(self):
        env = {k: v for k, v in os.environ.items() if k not in {"VISUAL", "EDITOR"}}
        with patch.dict(os.environ, env, clear=True):
            with patch(
                "src.cli.external_prompt_delegate.shutil.which",
                side_effect=lambda name: f"/usr/bin/{name}" if name == "vim" else None,
            ):
                self.assertEqual(_resolve_editor(), ["vim"])

    def test_falls_back_to_nano_when_vim_missing(self):
        env = {k: v for k, v in os.environ.items() if k not in {"VISUAL", "EDITOR"}}
        with patch.dict(os.environ, env, clear=True):
            with patch(
                "src.cli.external_prompt_delegate.shutil.which",
                side_effect=lambda name: f"/usr/bin/{name}" if name == "nano" else None,
            ):
                self.assertEqual(_resolve_editor(), ["nano"])


class LoadPromptFromFileTest(unittest.TestCase):

    def setUp(self):
        self.delegate = ExternalPromptDelegate()

    def test_happy_path_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.md"
            path.write_text("hello from a file", encoding="utf-8")
            result = self.delegate.load_prompt_from_file(str(path))

            self.assertTrue(result.success)
            self.assertEqual(result.prompt, "hello from a file")
            self.assertIsNone(result.errorMessage)

    def test_happy_path_txt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.txt"
            path.write_text("plain text prompt", encoding="utf-8")
            result = self.delegate.load_prompt_from_file(str(path))

            self.assertTrue(result.success)
            self.assertEqual(result.prompt, "plain text prompt")

    def test_strips_surrounding_whitespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.md"
            path.write_text("   spaced body   \n\n", encoding="utf-8")
            result = self.delegate.load_prompt_from_file(str(path))

            self.assertTrue(result.success)
            self.assertEqual(result.prompt, "spaced body")

    def test_rejects_missing_file(self):
        result = self.delegate.load_prompt_from_file("/nonexistent/path/file.md")

        self.assertFalse(result.success)
        self.assertIsNotNone(result.errorMessage)
        self.assertIn("Not a file", result.errorMessage)

    def test_rejects_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.delegate.load_prompt_from_file(tmp)

            self.assertFalse(result.success)
            self.assertIn("Not a file", result.errorMessage)

    def test_rejects_oversized_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "big.md"
            path.write_bytes(b"x" * (MAX_PROMPT_BYTES + 1))
            result = self.delegate.load_prompt_from_file(str(path))

            self.assertFalse(result.success)
            self.assertIn("too large", result.errorMessage)

    def test_accepts_at_size_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "limit.md"
            path.write_bytes(b"x" * MAX_PROMPT_BYTES)
            result = self.delegate.load_prompt_from_file(str(path))

            self.assertTrue(result.success)

    def test_rejects_invalid_utf8(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.md"
            path.write_bytes(b"\xff\xfe\x00\x80valid-ish")
            result = self.delegate.load_prompt_from_file(str(path))

            self.assertFalse(result.success)
            self.assertIn("UTF-8", result.errorMessage)

    def test_rejects_empty_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.md"
            path.write_text("   \n  \n", encoding="utf-8")
            result = self.delegate.load_prompt_from_file(str(path))

            self.assertFalse(result.success)
            self.assertIn("empty", result.errorMessage)

    def test_expands_user_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.md"
            path.write_text("home content", encoding="utf-8")

            with patch(
                "src.cli.external_prompt_delegate.Path.expanduser",
                return_value=path,
            ):
                result = self.delegate.load_prompt_from_file("~/input.md")

            self.assertTrue(result.success)
            self.assertEqual(result.prompt, "home content")

    def test_allowed_extensions_constant(self):
        self.assertEqual(ALLOWED_EXTENSIONS, {".md", ".txt"})


class ComposePromptTest(unittest.TestCase):

    def setUp(self):
        self.delegate = ExternalPromptDelegate()

    def _fake_editor_writing(self, payload: str) -> list[str]:
        script = (
            "import sys; "
            f"open(sys.argv[1], 'w', encoding='utf-8').write({payload!r})"
        )
        return [sys.executable, "-c", script]

    def test_editor_content_returned(self):
        fake = self._fake_editor_writing("hello from vim")

        with patch(
            "src.cli.external_prompt_delegate._resolve_editor", return_value=fake
        ):
            result = _run(self.delegate.compose_prompt())

        self.assertTrue(result.success)
        self.assertEqual(result.prompt, "hello from vim")

    def test_strips_seeded_header(self):
        fake = self._fake_editor_writing(
            "# my own comment\nactual prompt body\n# another\n"
        )

        with patch(
            "src.cli.external_prompt_delegate._resolve_editor", return_value=fake
        ):
            result = _run(self.delegate.compose_prompt())

        self.assertTrue(result.success)
        self.assertEqual(result.prompt, "actual prompt body")

    def test_empty_content_returns_blank(self):
        fake = self._fake_editor_writing("# only a comment\n   \n")

        with patch(
            "src.cli.external_prompt_delegate._resolve_editor", return_value=fake
        ):
            result = _run(self.delegate.compose_prompt())

        self.assertTrue(result.success)
        self.assertEqual(result.prompt, "")

    def test_editor_not_found(self):
        with patch(
            "src.cli.external_prompt_delegate._resolve_editor",
            return_value=["definitely-not-a-real-editor-xyz"],
        ):
            result = _run(self.delegate.compose_prompt())

        self.assertFalse(result.success)
        self.assertIn("Editor not found", result.errorMessage)

    def test_editor_nonzero_exit(self):
        fake = [sys.executable, "-c", "import sys; sys.exit(2)"]

        with patch(
            "src.cli.external_prompt_delegate._resolve_editor", return_value=fake
        ):
            result = _run(self.delegate.compose_prompt())

        self.assertFalse(result.success)
        self.assertIn("Editor exited with error", result.errorMessage)

    def test_temp_file_cleaned_up_on_success(self):
        captured: dict = {}
        original_run = __import__("subprocess").run

        def spy_run(cmd, *args, **kwargs):
            captured["path"] = cmd[-1]
            return original_run(cmd, *args, **kwargs)

        fake = self._fake_editor_writing("done")
        with patch(
            "src.cli.external_prompt_delegate._resolve_editor", return_value=fake
        ), patch(
            "src.cli.external_prompt_delegate.subprocess.run", side_effect=spy_run
        ):
            _run(self.delegate.compose_prompt())

        self.assertIn("path", captured)
        self.assertFalse(
            os.path.exists(captured["path"]),
            f"temp file {captured['path']} should have been deleted",
        )

    def test_temp_file_cleaned_up_on_editor_failure(self):
        captured: dict = {}
        original_run = __import__("subprocess").run

        def spy_run(cmd, *args, **kwargs):
            captured["path"] = cmd[-1]
            return original_run(cmd, *args, **kwargs)

        with patch(
            "src.cli.external_prompt_delegate._resolve_editor",
            return_value=[sys.executable, "-c", "import sys; sys.exit(1)"],
        ), patch(
            "src.cli.external_prompt_delegate.subprocess.run", side_effect=spy_run
        ):
            _run(self.delegate.compose_prompt())

        self.assertIn("path", captured)
        self.assertFalse(os.path.exists(captured["path"]))


if __name__ == "__main__":
    unittest.main()
