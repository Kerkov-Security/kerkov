"""Harness driver — spawns fresh Kerkov subprocesses in a loop.

Phase 1 contract:
  - task_list mode only (driver tolerates goal_predicate harness via warnings
    but only succeeds when task_list predicates pass)
  - Budgets enforced: max_iterations, max_wallclock_seconds
  - Launch gate: refuses target_class: external
  - Git init in run_dir on first launch; commit after each iteration
  - Predicate re-evaluation is authoritative — model self-report is advisory
  - Progress detection: a new file under artifacts/ since last iteration
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional, Set

from src.core.logging.logger_factory import get_logger
from src.harness.models import Harness, HarnessMode, HarnessStatus, TargetClass
from src.harness.parser import HarnessParseError, parse_harness
from src.harness.predicates import PredicateError, evaluate
from src.harness.state import load_state, save_state, state_path

log = get_logger("driver", log_level=logging.INFO, also_print_to_console=True)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_LAUNCH_GATE = 3
EXIT_BUDGET = 4
EXIT_STUCK = 5
EXIT_ITERATION_ERROR = 6


class DriverOutcome(str, Enum):
    COMPLETE = "complete"
    BUDGET_EXCEEDED = "budget_exceeded"
    STUCK = "stuck"
    ITERATION_ERROR = "iteration_error"
    LAUNCH_GATE = "launch_gate"
    INTERRUPTED = "interrupted"


@dataclass
class DriverArgs:
    run_dir: Path
    mode: str = "ctf"
    max_iterations_override: Optional[int] = None


@dataclass
class DriverContext:
    args: DriverArgs
    harness: Harness
    started_at: float = field(default_factory=time.monotonic)
    artifact_inventory: Set[str] = field(default_factory=set)


def driver_main(argv: List[str]) -> int:
    """Entry point invoked from `kerkov loop ...`."""
    args = _parse_args(argv)
    if args is None:
        return EXIT_USAGE

    try:
        harness, warnings = parse_harness(args.run_dir / "harness.md")
    except FileNotFoundError:
        log.error("harness.md not found in %s", args.run_dir)
        return EXIT_USAGE
    except HarnessParseError as exc:
        log.error("%s", exc)
        return EXIT_USAGE

    for warning in warnings:
        log.warning("%s", warning)

    if harness.target_class == TargetClass.EXTERNAL:
        log.error(
            "target_class 'external' is blocked in Phase 1. "
            "Tool-layer scope/rate enforcement (Phase 3) is a hard "
            "prerequisite for external-target runs."
        )
        return EXIT_LAUNCH_GATE

    _ensure_git_repo(args.run_dir)
    ctx = DriverContext(args=args, harness=harness)
    ctx.artifact_inventory = _scan_artifacts(args.run_dir)

    log.info(
        "harness '%s' loaded; target_class=%s",
        harness.name,
        harness.target_class.value,
    )
    log.info("run_dir: %s", args.run_dir)
    budget_iter = args.max_iterations_override or harness.budget.max_iterations
    budget_parts = [
        f"max_iterations={budget_iter}",
        f"max_wallclock_seconds={harness.budget.max_wallclock_seconds}",
    ]
    if harness.budget.max_tokens is not None:
        budget_parts.append(f"max_tokens={harness.budget.max_tokens}")
    if harness.budget.max_cost_usd is not None:
        budget_parts.append(f"max_cost_usd=${harness.budget.max_cost_usd:.2f}")
    log.info("budgets: %s", ", ".join(budget_parts))

    try:
        outcome = _loop(ctx)
    except KeyboardInterrupt:
        log.warning("interrupted by user")
        outcome = DriverOutcome.INTERRUPTED

    _write_summary(args.run_dir, ctx, outcome)
    log.info("outcome: %s", outcome.value)
    return _exit_code_for(outcome)


def _parse_args(argv: List[str]) -> Optional[DriverArgs]:
    parser = argparse.ArgumentParser(prog="kerkov loop", description="Run a harness loop.")
    parser.add_argument("run_dir", type=Path, help="Run directory containing harness.md")
    parser.add_argument(
        "--mode",
        type=str,
        default="ctf",
        choices=["recon", "audit", "offensive", "ctf"],
        help="Operational mode for the iteration subprocesses (default: ctf)",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="Override harness.budget.max_iterations (useful for smoke tests)",
    )
    try:
        ns = parser.parse_args(argv)
    except SystemExit:
        return None

    run_dir = ns.run_dir.resolve()
    if not run_dir.is_dir():
        log.error("run_dir does not exist: %s", run_dir)
        return None

    return DriverArgs(
        run_dir=run_dir,
        mode=ns.mode,
        max_iterations_override=ns.max_iterations,
    )


def _loop(ctx: DriverContext) -> DriverOutcome:
    state = load_state(ctx.args.run_dir)
    budget_iter = ctx.args.max_iterations_override or ctx.harness.budget.max_iterations

    while True:
        wallclock = time.monotonic() - ctx.started_at
        breach = _check_budgets(ctx.harness, state, wallclock, budget_iter)
        if breach is not None:
            log.warning("budget exceeded: %s", breach)
            return DriverOutcome.BUDGET_EXCEEDED

        if _all_predicates_pass(ctx.harness, ctx.args.run_dir):
            label = (
                "task" if ctx.harness.mode == HarnessMode.TASK_LIST else "success_criteria"
            )
            log.info(
                "all %s predicates pass after iteration %d",
                label,
                state.iteration,
            )
            return DriverOutcome.COMPLETE

        if _is_stuck(ctx.harness, state):
            log.warning(
                "stuck: %d iteration(s) without progress (>= stuck_after=%d)",
                state.iteration - state.last_progress_iteration,
                ctx.harness.stuck_after,
            )
            return DriverOutcome.STUCK

        next_iter_num = state.iteration + 1
        log.info("=== iteration %d ===", next_iter_num)
        proc_result = _spawn_iteration(ctx, next_iter_num)

        new_state = load_state(ctx.args.run_dir)
        # Guarantee monotonic iteration counter even if the subprocess abandoned
        # without calling HarnessStateTool.
        if new_state.iteration < next_iter_num:
            new_state.iteration = next_iter_num
            save_state(ctx.args.run_dir, new_state)

        new_inventory = _scan_artifacts(ctx.args.run_dir)
        new_files = new_inventory - ctx.artifact_inventory
        if new_files:
            new_state.last_progress_iteration = next_iter_num
            save_state(ctx.args.run_dir, new_state)
            log.info(
                "progress: %d new artifact(s) — %s",
                len(new_files),
                sorted(new_files),
            )
        ctx.artifact_inventory = new_inventory

        if ctx.harness.snapshot_per_iteration:
            _git_snapshot(ctx.args.run_dir, next_iter_num, proc_result.returncode)

        if proc_result.returncode >= 20:
            log.error(
                "iteration %d crashed (exit %d); stopping loop",
                next_iter_num,
                proc_result.returncode,
            )
            return DriverOutcome.ITERATION_ERROR

        if (
            new_state.status == HarnessStatus.COMPLETE
            and _all_predicates_pass(ctx.harness, ctx.args.run_dir)
        ):
            log.info(
                "iteration %d: model reported complete AND predicates agree",
                next_iter_num,
            )
            return DriverOutcome.COMPLETE

        if new_state.status == HarnessStatus.COMPLETE:
            log.warning(
                "iteration %d: model claimed complete but predicates disagree — continuing",
                next_iter_num,
            )
            for ident, expr, reason in _predicate_failures(
                ctx.harness, ctx.args.run_dir
            ):
                log.warning("  - %s: %s → %s", ident, expr, reason)

        state = new_state


def _check_budgets(
    harness: Harness, state, wallclock_seconds: float, budget_iter: int
) -> Optional[str]:
    """Return a breach description, or None if all budgets are within limits.

    Token and cost budgets are optional (None disables enforcement). Cost is
    only enforced when state.cost_tracking_enabled is True — once pricing is
    unknown for any iteration the cumulative cost number can no longer be
    trusted for budget enforcement.
    """
    if state.iteration >= budget_iter:
        return f"iteration {state.iteration} >= max_iterations {budget_iter}"

    if wallclock_seconds >= harness.budget.max_wallclock_seconds:
        return (
            f"wallclock {int(wallclock_seconds)}s >= "
            f"max_wallclock_seconds {harness.budget.max_wallclock_seconds}"
        )

    max_tokens = harness.budget.max_tokens
    if max_tokens is not None and state.tokens_used_total >= max_tokens:
        return f"tokens_used_total {state.tokens_used_total} >= max_tokens {max_tokens}"

    max_cost = harness.budget.max_cost_usd
    if (
        max_cost is not None
        and state.cost_tracking_enabled
        and state.cost_usd_total >= max_cost
    ):
        return (
            f"cost_usd_total ${state.cost_usd_total:.4f} >= "
            f"max_cost_usd ${max_cost:.4f}"
        )

    return None


def _all_predicates_pass(harness: Harness, run_dir: Path) -> bool:
    """Authoritative completion check, dispatched by mode."""
    if harness.mode == HarnessMode.TASK_LIST:
        return _all_tasks_pass(harness, run_dir)
    return _all_success_criteria_pass(harness, run_dir)


def _all_tasks_pass(harness: Harness, run_dir: Path) -> bool:
    if not harness.tasks:
        return False
    for task in harness.tasks:
        try:
            if not evaluate(task.done, run_dir):
                return False
        except PredicateError as exc:
            log.error("predicate error for task %r: %s", task.id, exc)
            return False
    return True


def _all_success_criteria_pass(harness: Harness, run_dir: Path) -> bool:
    if not harness.success_criteria:
        return False
    for crit in harness.success_criteria:
        try:
            if not evaluate(crit.check, run_dir):
                return False
        except PredicateError as exc:
            log.error(
                "predicate error for success_criterion %r: %s", crit.id, exc
            )
            return False
    return True


def _predicate_failures(harness: Harness, run_dir: Path) -> List[tuple]:
    """Return [(id, expr, reason)] for each predicate that did not pass.

    `reason` is "not satisfied" or "predicate error: <msg>" — used so the
    "model claimed complete but predicates disagree" log can name what's
    actually missing instead of forcing the user to walk the transcript.
    """
    if harness.mode == HarnessMode.TASK_LIST:
        items = [(t.id, t.done) for t in harness.tasks]
    else:
        items = [(c.id, c.check) for c in harness.success_criteria]

    failures: List[tuple] = []
    for ident, expr in items:
        try:
            if not evaluate(expr, run_dir):
                failures.append((ident, expr, "not satisfied"))
        except PredicateError as exc:
            failures.append((ident, expr, f"predicate error: {exc}"))
    return failures


def _is_stuck(harness: Harness, state) -> bool:
    """Return True when the run has gone stuck_after iterations without progress.

    Skipped at iteration 0 (nothing has run yet) and when stuck_after <= 0
    (effectively disables the check).
    """
    if state.iteration <= 0 or harness.stuck_after <= 0:
        return False
    return (state.iteration - state.last_progress_iteration) >= harness.stuck_after


def _scan_artifacts(run_dir: Path) -> Set[str]:
    artifacts_dir = run_dir / "artifacts"
    if not artifacts_dir.is_dir():
        return set()
    return {
        str(p.relative_to(run_dir))
        for p in artifacts_dir.rglob("*")
        if p.is_file()
    }


def _spawn_iteration(
    ctx: DriverContext, iteration_num: int
) -> subprocess.CompletedProcess:
    """Spawn a fresh `python -m src.cli.main --harness-iteration ...` subprocess."""
    env = os.environ.copy()
    env["ALLOW_AUTONOMOUS"] = "true"
    env["AUTONOMY_LEVEL"] = "4"

    cmd = [
        sys.executable,
        "-m",
        "src.cli.main",
        "--harness-iteration",
        str(ctx.args.run_dir),
        "--mode",
        ctx.args.mode,
        "--no-welcome",
    ]

    log.info("spawning: %s", " ".join(cmd))
    # stdout/stderr inherited so the user sees iteration output live
    return subprocess.run(cmd, env=env, check=False)


def _ensure_git_repo(run_dir: Path) -> None:
    """Initialize an isolated git repo inside the run dir if absent.

    Independent of any parent repo — these commits never pollute the surrounding
    project. Required by the state-reconstruction fallback (filesystem diff
    against the previous iteration's snapshot).
    """
    if (run_dir / ".git").exists():
        return
    log.info("initializing git repo in %s", run_dir)
    subprocess.run(["git", "init", "--quiet"], cwd=run_dir, check=True)
    # Local identity so commits don't fail when global config is absent.
    subprocess.run(
        ["git", "config", "user.email", "harness@kerkov.local"],
        cwd=run_dir,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Kerkov Harness Driver"],
        cwd=run_dir,
        check=True,
    )
    # Initial commit so subsequent diffs have a baseline.
    subprocess.run(["git", "add", "-A"], cwd=run_dir, check=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "harness: initial state"],
        cwd=run_dir,
        check=True,
    )


def _git_snapshot(run_dir: Path, iteration: int, exit_code: int) -> None:
    try:
        subprocess.run(["git", "add", "-A"], cwd=run_dir, check=True)
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=run_dir,
            check=False,
        )
        if result.returncode == 0:
            # Nothing staged — record an empty snapshot anyway so the iteration
            # ordinal is preserved in git history.
            subprocess.run(
                [
                    "git",
                    "commit",
                    "--allow-empty",
                    "-m",
                    f"harness: iteration {iteration} (exit {exit_code}, no changes)",
                ],
                cwd=run_dir,
                check=True,
            )
        else:
            subprocess.run(
                [
                    "git",
                    "commit",
                    "-m",
                    f"harness: iteration {iteration} (exit {exit_code})",
                ],
                cwd=run_dir,
                check=True,
            )
    except subprocess.CalledProcessError as exc:
        log.error("git snapshot failed: %s", exc)


def _write_summary(run_dir: Path, ctx: DriverContext, outcome: DriverOutcome) -> None:
    state = load_state(run_dir)
    wallclock = int(time.monotonic() - ctx.started_at)
    artifacts = sorted(_scan_artifacts(run_dir))

    cost_str = (
        f"${state.cost_usd_total:.4f}"
        if state.cost_tracking_enabled
        else "(unavailable — unknown model pricing)"
    )
    lines = [
        f"# Harness run summary",
        "",
        f"- **harness:** `{ctx.harness.name}`",
        f"- **mode:** `{ctx.harness.mode.value}`",
        f"- **outcome:** `{outcome.value}`",
        f"- **iterations:** {state.iteration}",
        f"- **wallclock:** {wallclock}s",
        f"- **tokens used:** {state.tokens_used_total}",
        f"- **cost:** {cost_str}",
        f"- **last progress iteration:** {state.last_progress_iteration}",
        f"- **last status reported by model:** `{state.status.value}`",
        f"- **last notes:** {state.notes or '(none)'}",
        "",
    ]

    lines.append("## Predicate status (driver-evaluated)")
    if ctx.harness.mode == HarnessMode.TASK_LIST:
        for task in ctx.harness.tasks:
            try:
                passed = evaluate(task.done, run_dir)
                marker = "x" if passed else " "
            except PredicateError:
                marker = "?"
            lines.append(f"- [{marker}] `{task.id}` — {task.description}")
    else:
        for crit in ctx.harness.success_criteria:
            try:
                passed = evaluate(crit.check, run_dir)
                marker = "x" if passed else " "
            except PredicateError:
                marker = "?"
            lines.append(f"- [{marker}] `{crit.id}` — {crit.description}")
    lines.append("")

    lines.append("## Artifacts")
    if artifacts:
        lines.extend(f"- `{a}`" for a in artifacts)
    else:
        lines.append("(none)")

    (run_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _exit_code_for(outcome: DriverOutcome) -> int:
    return {
        DriverOutcome.COMPLETE: EXIT_OK,
        DriverOutcome.BUDGET_EXCEEDED: EXIT_BUDGET,
        DriverOutcome.STUCK: EXIT_STUCK,
        DriverOutcome.ITERATION_ERROR: EXIT_ITERATION_ERROR,
        DriverOutcome.LAUNCH_GATE: EXIT_LAUNCH_GATE,
        DriverOutcome.INTERRUPTED: EXIT_USAGE,
    }[outcome]
