"""Predicate DSL evaluator.

Grammar (Phase 1, no parens):
    predicate := atom (' AND ' atom)*  ('OR' similar at lower precedence)
    expression := and_term (' OR ' and_term)*
    and_term   := atom (' AND ' atom)*
    atom       := file_exists:<path>
                | line_count:<path>:(>=|<=|==)<int>
                | line_match:<path>:<regex>
                | content_match:<path>:<regex>

All paths are evaluated relative to a base directory (the run_dir). Predicates
NEVER execute shell or load arbitrary code — they are pure filesystem checks.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

_OP_RE = re.compile(r"^(>=|<=|==)(\d+)$")


class PredicateError(ValueError):
    pass


def evaluate(expression: str, base: Path) -> bool:
    """Evaluate a predicate expression against base directory."""
    expression = expression.strip()
    if not expression:
        raise PredicateError("empty predicate")

    # OR has lowest precedence — split first.
    or_parts = _split_top(expression, " OR ")
    return any(_eval_and_chain(part, base) for part in or_parts)


def _eval_and_chain(expression: str, base: Path) -> bool:
    and_parts = _split_top(expression, " AND ")
    return all(_eval_atom(part.strip(), base) for part in and_parts)


def _split_top(expression: str, sep: str) -> list[str]:
    """Phase 1: no parens, no nesting — plain split on the separator."""
    return expression.split(sep)


def _eval_atom(atom: str, base: Path) -> bool:
    if not atom:
        raise PredicateError("empty atom")

    head, _, rest = atom.partition(":")
    if not rest:
        raise PredicateError(f"atom missing ':' separator: {atom!r}")

    handler = _ATOM_HANDLERS.get(head)
    if handler is None:
        raise PredicateError(f"unknown predicate kind: {head!r}")

    return handler(rest, base)


def _eval_file_exists(rest: str, base: Path) -> bool:
    target = (base / rest.strip()).resolve()
    return target.is_file()


def _eval_line_count(rest: str, base: Path) -> bool:
    path_str, _, op_str = rest.rpartition(":")
    if not path_str:
        raise PredicateError(f"line_count missing operator: line_count:{rest}")
    match = _OP_RE.match(op_str.strip())
    if not match:
        raise PredicateError(f"line_count operator must be >=N, <=N, or ==N: {op_str!r}")
    op, value_str = match.group(1), match.group(2)
    value = int(value_str)

    target = (base / path_str.strip()).resolve()
    if not target.is_file():
        return False
    actual = sum(1 for _ in target.open("r", encoding="utf-8", errors="replace"))

    if op == ">=":
        return actual >= value
    if op == "<=":
        return actual <= value
    return actual == value


def _eval_line_match(rest: str, base: Path) -> bool:
    path_str, _, pattern = rest.partition(":")
    if not pattern:
        raise PredicateError(f"line_match missing regex: line_match:{rest}")
    target = (base / path_str.strip()).resolve()
    if not target.is_file():
        return False
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise PredicateError(f"line_match invalid regex {pattern!r}: {exc}") from exc
    with target.open("r", encoding="utf-8", errors="replace") as fh:
        return any(compiled.search(line) for line in fh)


def _eval_content_match(rest: str, base: Path) -> bool:
    path_str, _, pattern = rest.partition(":")
    if not pattern:
        raise PredicateError(f"content_match missing regex: content_match:{rest}")
    target = (base / path_str.strip()).resolve()
    if not target.is_file():
        return False
    try:
        compiled = re.compile(pattern, re.DOTALL)
    except re.error as exc:
        raise PredicateError(f"content_match invalid regex {pattern!r}: {exc}") from exc
    return bool(compiled.search(target.read_text(encoding="utf-8", errors="replace")))


_ATOM_HANDLERS: dict[str, Callable[[str, Path], bool]] = {
    "file_exists": _eval_file_exists,
    "line_count": _eval_line_count,
    "line_match": _eval_line_match,
    "content_match": _eval_content_match,
}
