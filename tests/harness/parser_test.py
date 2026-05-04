"""Tests for the harness.md parser."""

import tempfile
import textwrap
import unittest
from pathlib import Path

from src.harness.models import HarnessMode, TargetClass
from src.harness.parser import HarnessParseError, parse_harness


def _write_harness(dir_: Path, content: str) -> Path:
    path = dir_ / "harness.md"
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
    return path


class ParseHarnessTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_minimal_task_list(self):
        path = _write_harness(
            self.dir,
            """
            ---
            name: minimal
            mode: task_list
            target_class: ctf
            tasks:
              - id: t1
                description: do thing
                done: "file_exists:./artifacts/proof.txt"
            ---
            body text
            """,
        )
        harness, warnings = parse_harness(path)
        self.assertEqual(harness.name, "minimal")
        self.assertEqual(harness.mode, HarnessMode.TASK_LIST)
        self.assertEqual(harness.target_class, TargetClass.CTF)
        self.assertEqual(len(harness.tasks), 1)
        self.assertEqual(harness.tasks[0].id, "t1")
        self.assertFalse(harness.tasks[0].passes)
        self.assertIn("body text", harness.body)
        self.assertEqual(warnings, [])

    def test_task_list_requires_at_least_one_task(self):
        path = _write_harness(
            self.dir,
            """
            ---
            name: empty
            mode: task_list
            target_class: ctf
            tasks: []
            ---
            """,
        )
        with self.assertRaises(HarnessParseError):
            parse_harness(path)

    def test_missing_frontmatter_rejected(self):
        path = self.dir / "harness.md"
        path.write_text("no frontmatter here\n", encoding="utf-8")
        with self.assertRaises(HarnessParseError):
            parse_harness(path)

    def test_external_target_parses(self):
        # Parser accepts it; the driver enforces the launch gate.
        path = _write_harness(
            self.dir,
            """
            ---
            name: ext
            mode: task_list
            target_class: external
            tasks:
              - id: t1
                description: x
                done: "file_exists:x"
            ---
            """,
        )
        harness, _ = parse_harness(path)
        self.assertEqual(harness.target_class, TargetClass.EXTERNAL)

    def test_goal_predicate_requires_success_criteria(self):
        path = _write_harness(
            self.dir,
            """
            ---
            name: gp
            mode: goal_predicate
            target_class: ctf
            goal: "find the bug"
            ---
            """,
        )
        with self.assertRaises(HarnessParseError):
            parse_harness(path)

    def test_goal_predicate_with_criteria(self):
        path = _write_harness(
            self.dir,
            """
            ---
            name: gp
            mode: goal_predicate
            target_class: ctf
            goal: "find the bug"
            success_criteria:
              - id: poc_exists
                description: PoC script exists
                check: "file_exists:./artifacts/poc.py"
              - id: writeup_exists
                description: bug writeup exists
                check: "file_exists:./findings/bug-01.md"
            ---
            """,
        )
        harness, warnings = parse_harness(path)
        self.assertEqual(harness.mode, HarnessMode.GOAL_PREDICATE)
        self.assertEqual(len(harness.success_criteria), 2)
        self.assertEqual(harness.success_criteria[0].id, "poc_exists")
        self.assertEqual(warnings, [])

    def test_duplicate_task_ids_rejected(self):
        path = _write_harness(
            self.dir,
            """
            ---
            name: dupe
            mode: task_list
            target_class: ctf
            tasks:
              - id: t1
                description: a
                done: "file_exists:a"
              - id: t1
                description: b
                done: "file_exists:b"
            ---
            """,
        )
        with self.assertRaisesRegex(HarnessParseError, "duplicate task ids"):
            parse_harness(path)

    def test_duplicate_criterion_ids_rejected(self):
        path = _write_harness(
            self.dir,
            """
            ---
            name: gpdupe
            mode: goal_predicate
            target_class: ctf
            goal: x
            success_criteria:
              - id: c1
                description: a
                check: "file_exists:a"
              - id: c1
                description: b
                check: "file_exists:b"
            ---
            """,
        )
        with self.assertRaisesRegex(HarnessParseError, "duplicate success_criteria ids"):
            parse_harness(path)

    def test_token_and_cost_budgets_parse_without_warnings(self):
        path = _write_harness(
            self.dir,
            """
            ---
            name: w
            mode: task_list
            target_class: ctf
            budget:
              max_iterations: 5
              max_tokens: 1000000
              max_cost_usd: 5.0
            tasks:
              - id: t1
                description: x
                done: "file_exists:x"
            ---
            """,
        )
        harness, warnings = parse_harness(path)
        self.assertEqual(warnings, [])
        self.assertEqual(harness.budget.max_tokens, 1_000_000)
        self.assertEqual(harness.budget.max_cost_usd, 5.0)


if __name__ == "__main__":
    unittest.main()
