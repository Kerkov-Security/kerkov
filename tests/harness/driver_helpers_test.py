"""Tests for driver helpers that don't require subprocess spawning."""

import asyncio
import json
import tempfile
import textwrap
import unittest
from pathlib import Path

from src.harness import driver
from src.harness.models import (
    Budget,
    Harness,
    HarnessMode,
    HarnessStatus,
    SuccessCriterion,
    Task,
    TargetClass,
)
from src.harness.parser import parse_harness
from src.harness.state import load_state, save_state
from src.harness.models import HarnessState


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class AllTasksPassTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _harness(self, *predicates: str) -> Harness:
        return Harness(
            name="t",
            target_class=TargetClass.LOCAL,
            tasks=[
                Task(id=f"t{i}", description="x", done=p)
                for i, p in enumerate(predicates)
            ],
        )

    def test_no_tasks_returns_false(self):
        harness = Harness(name="t", target_class=TargetClass.LOCAL, tasks=[])
        self.assertFalse(driver._all_tasks_pass(harness, self.dir))

    def test_all_pass(self):
        _write(self.dir / "a.txt", "1\n")
        _write(self.dir / "b.txt", "2\n")
        harness = self._harness("file_exists:a.txt", "file_exists:b.txt")
        self.assertTrue(driver._all_tasks_pass(harness, self.dir))

    def test_one_fails(self):
        _write(self.dir / "a.txt", "1\n")
        harness = self._harness("file_exists:a.txt", "file_exists:missing.txt")
        self.assertFalse(driver._all_tasks_pass(harness, self.dir))


class ScanArtifactsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_empty_returns_empty(self):
        self.assertEqual(driver._scan_artifacts(self.dir), set())

    def test_scans_recursively(self):
        _write(self.dir / "artifacts" / "a.txt", "x")
        _write(self.dir / "artifacts" / "sub" / "b.txt", "y")
        _write(self.dir / "other" / "c.txt", "z")  # not under artifacts
        result = driver._scan_artifacts(self.dir)
        self.assertEqual(
            result,
            {
                str(Path("artifacts") / "a.txt"),
                str(Path("artifacts") / "sub" / "b.txt"),
            },
        )


class StateRoundtripTest(unittest.TestCase):
    def test_save_then_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            state = HarnessState(
                iteration=3,
                status=HarnessStatus.CONTINUE,
                last_progress_iteration=2,
                tokens_used_total=12345,
                notes="some notes",
            )
            save_state(d, state)
            loaded = load_state(d)
            self.assertEqual(loaded.iteration, 3)
            self.assertEqual(loaded.status, HarnessStatus.CONTINUE)
            self.assertEqual(loaded.notes, "some notes")
            self.assertEqual(loaded.tokens_used_total, 12345)

    def test_load_missing_returns_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            loaded = load_state(Path(tmp))
            self.assertEqual(loaded.iteration, 0)
            self.assertEqual(loaded.status, HarnessStatus.CONTINUE)


class AllSuccessCriteriaPassTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _harness(self, *checks: str) -> Harness:
        return Harness(
            name="t",
            mode=HarnessMode.GOAL_PREDICATE,
            target_class=TargetClass.LOCAL,
            goal="x",
            success_criteria=[
                SuccessCriterion(id=f"c{i}", description="x", check=c)
                for i, c in enumerate(checks)
            ],
        )

    def test_no_criteria_returns_false(self):
        harness = Harness(
            name="t",
            mode=HarnessMode.GOAL_PREDICATE,
            target_class=TargetClass.LOCAL,
            goal="x",
        )
        self.assertFalse(driver._all_success_criteria_pass(harness, self.dir))

    def test_all_pass(self):
        _write(self.dir / "a.txt", "1\n")
        _write(self.dir / "b.txt", "2\n")
        harness = self._harness("file_exists:a.txt", "file_exists:b.txt")
        self.assertTrue(driver._all_success_criteria_pass(harness, self.dir))

    def test_one_fails(self):
        _write(self.dir / "a.txt", "1\n")
        harness = self._harness("file_exists:a.txt", "file_exists:missing.txt")
        self.assertFalse(driver._all_success_criteria_pass(harness, self.dir))


class AllPredicatesPassDispatchTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_dispatches_to_tasks_for_task_list(self):
        _write(self.dir / "a.txt", "1\n")
        harness = Harness(
            name="t",
            mode=HarnessMode.TASK_LIST,
            target_class=TargetClass.LOCAL,
            tasks=[Task(id="t1", description="x", done="file_exists:a.txt")],
        )
        self.assertTrue(driver._all_predicates_pass(harness, self.dir))

    def test_dispatches_to_criteria_for_goal_predicate(self):
        _write(self.dir / "a.txt", "1\n")
        harness = Harness(
            name="t",
            mode=HarnessMode.GOAL_PREDICATE,
            target_class=TargetClass.LOCAL,
            goal="x",
            success_criteria=[
                SuccessCriterion(id="c1", description="x", check="file_exists:a.txt")
            ],
        )
        self.assertTrue(driver._all_predicates_pass(harness, self.dir))


class PredicateFailuresTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_task_list_returns_empty_when_all_pass(self):
        _write(self.dir / "a.txt", "1\n")
        harness = Harness(
            name="t",
            mode=HarnessMode.TASK_LIST,
            target_class=TargetClass.LOCAL,
            tasks=[Task(id="t1", description="x", done="file_exists:a.txt")],
        )
        self.assertEqual(driver._predicate_failures(harness, self.dir), [])

    def test_task_list_reports_missing_predicate(self):
        # Mirrors the smoke-ctf failure mode: model wrote proof.txt but not
        # recon.md / vuln_class.md.
        _write(self.dir / "artifacts" / "proof.txt", "picoCTF{x}\n")
        harness = Harness(
            name="smoke",
            mode=HarnessMode.TASK_LIST,
            target_class=TargetClass.CTF,
            tasks=[
                Task(
                    id="enumerate",
                    description="recon",
                    done="file_exists:./artifacts/recon.md",
                ),
                Task(
                    id="identify_vuln",
                    description="vuln",
                    done="file_exists:./artifacts/vuln_class.md",
                ),
                Task(
                    id="capture_flag",
                    description="flag",
                    done="line_match:./artifacts/proof.txt:picoCTF\\{.*\\}",
                ),
            ],
        )
        failures = driver._predicate_failures(harness, self.dir)
        ids = [f[0] for f in failures]
        reasons = {f[0]: f[2] for f in failures}
        self.assertEqual(ids, ["enumerate", "identify_vuln"])
        self.assertEqual(reasons["enumerate"], "not satisfied")
        self.assertEqual(reasons["identify_vuln"], "not satisfied")

    def test_goal_predicate_reports_failures(self):
        _write(self.dir / "a.txt", "1\n")
        harness = Harness(
            name="t",
            mode=HarnessMode.GOAL_PREDICATE,
            target_class=TargetClass.LOCAL,
            goal="x",
            success_criteria=[
                SuccessCriterion(id="c1", description="x", check="file_exists:a.txt"),
                SuccessCriterion(
                    id="c2", description="x", check="file_exists:missing.txt"
                ),
            ],
        )
        failures = driver._predicate_failures(harness, self.dir)
        self.assertEqual([f[0] for f in failures], ["c2"])

    def test_predicate_error_is_reported_not_raised(self):
        harness = Harness(
            name="t",
            mode=HarnessMode.TASK_LIST,
            target_class=TargetClass.LOCAL,
            tasks=[Task(id="t1", description="x", done="bogus_kind:./x")],
        )
        failures = driver._predicate_failures(harness, self.dir)
        self.assertEqual(len(failures), 1)
        ident, expr, reason = failures[0]
        self.assertEqual(ident, "t1")
        self.assertEqual(expr, "bogus_kind:./x")
        self.assertTrue(reason.startswith("predicate error:"))


class IsStuckTest(unittest.TestCase):
    def _harness(self, stuck_after: int = 8) -> Harness:
        return Harness(
            name="t",
            mode=HarnessMode.GOAL_PREDICATE,
            target_class=TargetClass.LOCAL,
            goal="x",
            stuck_after=stuck_after,
            success_criteria=[
                SuccessCriterion(id="c1", description="x", check="file_exists:nope")
            ],
        )

    def test_iteration_zero_is_not_stuck(self):
        state = HarnessState(iteration=0, last_progress_iteration=0)
        self.assertFalse(driver._is_stuck(self._harness(), state))

    def test_within_threshold_is_not_stuck(self):
        state = HarnessState(iteration=7, last_progress_iteration=0)
        self.assertFalse(driver._is_stuck(self._harness(8), state))

    def test_at_threshold_is_stuck(self):
        state = HarnessState(iteration=8, last_progress_iteration=0)
        self.assertTrue(driver._is_stuck(self._harness(8), state))

    def test_recent_progress_is_not_stuck(self):
        state = HarnessState(iteration=10, last_progress_iteration=8)
        self.assertFalse(driver._is_stuck(self._harness(8), state))

    def test_stuck_after_zero_disables_check(self):
        state = HarnessState(iteration=100, last_progress_iteration=0)
        self.assertFalse(driver._is_stuck(self._harness(0), state))


class CheckBudgetsTest(unittest.TestCase):
    def _harness(self, **budget_kwargs) -> Harness:
        return Harness(
            name="t",
            mode=HarnessMode.TASK_LIST,
            target_class=TargetClass.LOCAL,
            budget=Budget(**budget_kwargs),
            tasks=[Task(id="t1", description="x", done="file_exists:nope")],
        )

    def test_within_all_budgets(self):
        h = self._harness(max_iterations=10, max_wallclock_seconds=3600)
        state = HarnessState(iteration=2, tokens_used_total=100)
        self.assertIsNone(driver._check_budgets(h, state, 60.0, 10))

    def test_iteration_breach(self):
        h = self._harness(max_iterations=5, max_wallclock_seconds=3600)
        state = HarnessState(iteration=5)
        self.assertIn("max_iterations", driver._check_budgets(h, state, 0, 5))

    def test_wallclock_breach(self):
        h = self._harness(max_iterations=10, max_wallclock_seconds=120)
        state = HarnessState(iteration=1)
        self.assertIn("wallclock", driver._check_budgets(h, state, 121.0, 10))

    def test_token_breach(self):
        h = self._harness(
            max_iterations=10, max_wallclock_seconds=3600, max_tokens=1000
        )
        state = HarnessState(iteration=2, tokens_used_total=1500)
        self.assertIn("max_tokens", driver._check_budgets(h, state, 1.0, 10))

    def test_token_unset_disables_check(self):
        h = self._harness(max_iterations=10, max_wallclock_seconds=3600)
        state = HarnessState(iteration=2, tokens_used_total=10_000_000_000)
        self.assertIsNone(driver._check_budgets(h, state, 1.0, 10))

    def test_cost_breach(self):
        h = self._harness(
            max_iterations=10, max_wallclock_seconds=3600, max_cost_usd=5.0
        )
        state = HarnessState(
            iteration=2, cost_usd_total=5.50, cost_tracking_enabled=True
        )
        self.assertIn("max_cost_usd", driver._check_budgets(h, state, 1.0, 10))

    def test_cost_not_enforced_when_tracking_disabled(self):
        # Once an unknown-pricing iteration runs, cost cannot be enforced even
        # if cost_usd_total looks high (it's not trustworthy).
        h = self._harness(
            max_iterations=10, max_wallclock_seconds=3600, max_cost_usd=1.0
        )
        state = HarnessState(
            iteration=2, cost_usd_total=99.0, cost_tracking_enabled=False
        )
        self.assertIsNone(driver._check_budgets(h, state, 1.0, 10))


class HarnessStateToolTest(unittest.TestCase):
    def setUp(self):
        from src.tools.harness_state_tool import HarnessStateTool

        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.harness = Harness(
            name="t",
            mode=HarnessMode.TASK_LIST,
            target_class=TargetClass.LOCAL,
            tasks=[
                Task(id="enumerate", description="x", done="file_exists:a"),
                Task(id="exploit", description="x", done="file_exists:b"),
            ],
        )
        self.usage = (1000, 500)
        self.tool = HarnessStateTool(
            self.dir,
            current_iteration=7,
            harness=self.harness,
            usage_provider=lambda: self.usage,
            model_name="claude-sonnet-4-5",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_writes_state_json_with_terminal_metadata(self):
        result = asyncio.run(
            self.tool.execute({"status": "continue", "notes": "ok"})
        )
        self.assertTrue(result.success)
        self.assertTrue(result.metadata.get("terminal"))

        state = load_state(self.dir)
        self.assertEqual(state.iteration, 7)
        self.assertEqual(state.status, HarnessStatus.CONTINUE)
        self.assertEqual(state.notes, "ok")
        # Authoritative tokens come from the usage_provider (1000 + 500), not
        # from any model-supplied param.
        self.assertEqual(state.tokens_used_this_iteration, 1500)
        self.assertEqual(state.tokens_used_total, 1500)
        self.assertTrue(state.cost_tracking_enabled)
        # 1000 input * $3/M + 500 output * $15/M = 0.003 + 0.0075 = 0.0105
        self.assertAlmostEqual(state.cost_usd_this_iteration, 0.0105, places=6)
        self.assertAlmostEqual(state.cost_usd_total, 0.0105, places=6)

    def test_cumulative_totals_merge_across_iterations(self):
        # First iteration writes 1500 tokens.
        asyncio.run(self.tool.execute({"status": "continue"}))

        # Second iteration: change usage and iteration ordinal, expect cumulative.
        from src.tools.harness_state_tool import HarnessStateTool

        self.usage = (2000, 1000)
        next_tool = HarnessStateTool(
            self.dir,
            current_iteration=8,
            harness=self.harness,
            usage_provider=lambda: self.usage,
            model_name="claude-sonnet-4-5",
        )
        result = asyncio.run(next_tool.execute({"status": "continue"}))
        self.assertTrue(result.success)

        state = load_state(self.dir)
        self.assertEqual(state.iteration, 8)
        self.assertEqual(state.tokens_used_this_iteration, 3000)
        self.assertEqual(state.tokens_used_total, 4500)  # 1500 + 3000

    def test_unknown_model_disables_cost_tracking(self):
        from src.tools.harness_state_tool import HarnessStateTool

        tool = HarnessStateTool(
            self.dir,
            current_iteration=1,
            harness=self.harness,
            usage_provider=lambda: (100, 50),
            model_name="never-heard-of-this-model",
        )
        result = asyncio.run(tool.execute({"status": "continue"}))
        self.assertTrue(result.success)

        state = load_state(self.dir)
        self.assertFalse(state.cost_tracking_enabled)
        self.assertEqual(state.cost_usd_this_iteration, 0.0)
        # Tokens are still tracked even when pricing is unknown
        self.assertEqual(state.tokens_used_this_iteration, 150)

    def test_cost_tracking_stays_disabled_once_disabled(self):
        # Sticky: once an iteration ran with unknown pricing, cumulative cost
        # is no longer trustworthy even if a later iteration knows the model.
        from src.tools.harness_state_tool import HarnessStateTool

        tool_unknown = HarnessStateTool(
            self.dir,
            current_iteration=1,
            harness=self.harness,
            usage_provider=lambda: (100, 50),
            model_name="unknown-model",
        )
        asyncio.run(tool_unknown.execute({"status": "continue"}))

        tool_known = HarnessStateTool(
            self.dir,
            current_iteration=2,
            harness=self.harness,
            usage_provider=lambda: (100, 50),
            model_name="claude-sonnet-4-5",
        )
        asyncio.run(tool_known.execute({"status": "continue"}))

        state = load_state(self.dir)
        self.assertFalse(state.cost_tracking_enabled)

    def test_invalid_status_rejected(self):
        result = asyncio.run(
            self.tool.execute({"status": "not_a_real_status"})
        )
        self.assertFalse(result.success)
        self.assertFalse((self.dir / "state.json").exists())

    def test_unknown_task_id_rejected(self):
        result = asyncio.run(
            self.tool.execute(
                {"status": "continue", "tasks_passed": ["enumerate", "ghost"]}
            )
        )
        self.assertFalse(result.success)
        self.assertIn("ghost", result.error)
        self.assertFalse((self.dir / "state.json").exists())

    def test_known_task_id_accepted(self):
        result = asyncio.run(
            self.tool.execute(
                {"status": "continue", "tasks_passed": ["enumerate"]}
            )
        )
        self.assertTrue(result.success)

    def test_stuck_requires_forfeit_reason(self):
        result = asyncio.run(
            self.tool.execute({"status": "stuck"})
        )
        self.assertFalse(result.success)
        self.assertIn("forfeit_reason", result.error)

    def test_stuck_with_forfeit_reason_accepted(self):
        result = asyncio.run(
            self.tool.execute(
                {"status": "stuck", "forfeit_reason": "no exploit path found"}
            )
        )
        self.assertTrue(result.success)
        state = load_state(self.dir)
        self.assertEqual(state.status, HarnessStatus.STUCK)
        self.assertIn("forfeit_reason: no exploit path found", state.notes)

    def test_unknown_criterion_id_rejected(self):
        gp_harness = Harness(
            name="t",
            mode=HarnessMode.GOAL_PREDICATE,
            target_class=TargetClass.LOCAL,
            goal="x",
            success_criteria=[
                SuccessCriterion(id="poc_exists", description="x", check="file_exists:p")
            ],
        )
        from src.tools.harness_state_tool import HarnessStateTool
        tool = HarnessStateTool(
            self.dir,
            current_iteration=1,
            harness=gp_harness,
            usage_provider=lambda: (0, 0),
            model_name="claude-sonnet-4-5",
        )
        result = asyncio.run(
            tool.execute(
                {
                    "status": "continue",
                    "success_criteria_results": [{"id": "made_up", "met": True}],
                }
            )
        )
        self.assertFalse(result.success)
        self.assertIn("made_up", result.error)


if __name__ == "__main__":
    unittest.main()
