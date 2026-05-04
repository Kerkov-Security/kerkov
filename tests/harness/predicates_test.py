"""Tests for the predicate DSL evaluator."""

import tempfile
import unittest
from pathlib import Path

from src.harness.predicates import PredicateError, evaluate


class PredicateAtomTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, rel: str, content: str) -> None:
        path = self.base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_file_exists_true(self):
        self._write("artifacts/proof.txt", "hi\n")
        self.assertTrue(evaluate("file_exists:artifacts/proof.txt", self.base))

    def test_file_exists_false(self):
        self.assertFalse(evaluate("file_exists:missing.txt", self.base))

    def test_line_count_gte(self):
        self._write("a.txt", "1\n2\n3\n")
        self.assertTrue(evaluate("line_count:a.txt:>=3", self.base))
        self.assertTrue(evaluate("line_count:a.txt:>=2", self.base))
        self.assertFalse(evaluate("line_count:a.txt:>=4", self.base))

    def test_line_count_eq_and_lte(self):
        self._write("a.txt", "1\n2\n")
        self.assertTrue(evaluate("line_count:a.txt:==2", self.base))
        self.assertTrue(evaluate("line_count:a.txt:<=2", self.base))
        self.assertFalse(evaluate("line_count:a.txt:==3", self.base))

    def test_line_count_missing_file_is_false(self):
        self.assertFalse(evaluate("line_count:missing.txt:>=1", self.base))

    def test_line_match(self):
        self._write("artifacts/proof.txt", "garbage\nflag{abc123}\nmore\n")
        self.assertTrue(
            evaluate(r"line_match:artifacts/proof.txt:flag\{.*\}", self.base)
        )
        self.assertFalse(
            evaluate(r"line_match:artifacts/proof.txt:nope_pattern", self.base)
        )

    def test_content_match_spans_lines(self):
        self._write("doc.md", "first line\nsecond line\n")
        self.assertTrue(
            evaluate(r"content_match:doc.md:first.*second", self.base)
        )


class PredicateCompositionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        (self.base / "a.txt").write_text("x\n", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_and_both_true(self):
        self.assertTrue(
            evaluate("file_exists:a.txt AND line_count:a.txt:>=1", self.base)
        )

    def test_and_one_false(self):
        self.assertFalse(
            evaluate("file_exists:a.txt AND file_exists:missing.txt", self.base)
        )

    def test_or_one_true(self):
        self.assertTrue(
            evaluate("file_exists:missing.txt OR file_exists:a.txt", self.base)
        )

    def test_or_both_false(self):
        self.assertFalse(
            evaluate("file_exists:missing.txt OR file_exists:also_missing.txt", self.base)
        )

    def test_or_binds_lower_than_and(self):
        self.assertTrue(
            evaluate(
                "file_exists:missing.txt AND file_exists:a.txt OR file_exists:a.txt",
                self.base,
            )
        )


class PredicateErrorTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_empty_predicate(self):
        with self.assertRaises(PredicateError):
            evaluate("", self.base)

    def test_unknown_kind(self):
        with self.assertRaises(PredicateError):
            evaluate("nope:something", self.base)

    def test_atom_missing_separator(self):
        with self.assertRaises(PredicateError):
            evaluate("file_exists", self.base)

    def test_line_count_bad_operator(self):
        (self.base / "a.txt").write_text("x\n", encoding="utf-8")
        with self.assertRaises(PredicateError):
            evaluate("line_count:a.txt:>3", self.base)


if __name__ == "__main__":
    unittest.main()
