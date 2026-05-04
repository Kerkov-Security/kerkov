"""Parse harness.md — YAML frontmatter + markdown body."""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Tuple

import yaml

from src.harness.models import Harness, HarnessMode, TargetClass


class HarnessParseError(ValueError):
    pass


_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z",
    re.DOTALL,
)


def parse_harness(path: Path) -> Tuple[Harness, List[str]]:
    """Parse a harness.md file.

    Returns (harness, warnings). Raises HarnessParseError on hard failures.
    """
    raw = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        raise HarnessParseError(
            f"{path}: missing or malformed YAML frontmatter "
            "(expected file to start with '---' on its own line)"
        )

    frontmatter_text, body = match.group(1), match.group(2)
    try:
        data = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError as exc:
        raise HarnessParseError(f"{path}: invalid YAML frontmatter: {exc}") from exc

    if not isinstance(data, dict):
        raise HarnessParseError(
            f"{path}: frontmatter must be a YAML mapping, got {type(data).__name__}"
        )

    data["body"] = body
    warnings: List[str] = []

    try:
        harness = Harness(**data)
    except Exception as exc:
        raise HarnessParseError(f"{path}: invalid harness fields: {exc}") from exc

    if harness.mode == HarnessMode.TASK_LIST and not harness.tasks:
        raise HarnessParseError(f"{path}: task_list mode requires at least one task")

    if harness.mode == HarnessMode.GOAL_PREDICATE and not harness.success_criteria:
        raise HarnessParseError(
            f"{path}: goal_predicate mode requires at least one success_criteria entry"
        )

    _check_unique_ids(path, harness)

    return harness, warnings


def _check_unique_ids(path: Path, harness: Harness) -> None:
    """Reject duplicate task IDs or success_criteria IDs.

    The HarnessStateTool cross-validates self-report IDs against these sets, so
    duplicates would make self-report ambiguous.
    """
    task_ids = [t.id for t in harness.tasks]
    if len(set(task_ids)) != len(task_ids):
        dupes = sorted({tid for tid in task_ids if task_ids.count(tid) > 1})
        raise HarnessParseError(f"{path}: duplicate task ids: {dupes}")

    crit_ids = [c.id for c in harness.success_criteria]
    if len(set(crit_ids)) != len(crit_ids):
        dupes = sorted({cid for cid in crit_ids if crit_ids.count(cid) > 1})
        raise HarnessParseError(f"{path}: duplicate success_criteria ids: {dupes}")


def is_external_target(harness: Harness) -> bool:
    """Phase 1 launch gate helper."""
    return harness.target_class == TargetClass.EXTERNAL
