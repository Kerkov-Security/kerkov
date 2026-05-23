"""
Pattern-based tool approval rules engine.

Rules are evaluated in order; first match wins. If no rule matches the caller
decides what to do (typically: prompt the user).

Rule syntax in YAML:
  rules:
    - allow: exec({'command': 'ping *'})
    - deny:  exec({'command': 're:.*sudo.*'})

Only the fields listed in the rule dict need to match; extra params on the
actual call are ignored (partial match). Patterns are glob by default; prefix
a value with ``re:`` to use a regular expression instead.
"""

import ast
import fnmatch
import re
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

import yaml
from pydantic import BaseModel

from src.core.logging.logger_factory import get_logger

logger = get_logger("ApprovalRules")

RuleAction = Literal["allow", "deny"]

DEFAULT_APPROVALS_PATH = "config/approvals.yaml"


class ApprovalRule(BaseModel):
    action: RuleAction
    tool: str
    params: Dict[str, str]


def _parse_rule_string(rule_string: str) -> Tuple[str, Dict[str, str]]:
    """Parse ``tool_name({'field': 'pattern', ...})`` into ``(tool_name, params)``."""
    rule_string = rule_string.strip()
    try:
        paren_index = rule_string.index("(")
    except ValueError:
        raise ValueError(f"Rule string missing opening paren: {rule_string!r}")

    tool_name = rule_string[:paren_index].strip()
    if not tool_name:
        raise ValueError(f"Rule string missing tool name: {rule_string!r}")

    dict_str = rule_string[paren_index + 1 :].rstrip(")")
    dict_str = dict_str.strip()
    if not dict_str:
        return tool_name, {}

    try:
        parsed = ast.literal_eval(dict_str)
    except (ValueError, SyntaxError) as exc:
        raise ValueError(
            f"Could not parse params dict in rule {rule_string!r}: {exc}"
        ) from exc

    if not isinstance(parsed, dict):
        raise ValueError(
            f"Expected dict in rule params, got {type(parsed).__name__}: {rule_string!r}"
        )

    return tool_name, {k: str(v) for k, v in parsed.items()}


def parse_rules(rules_data: List[Any]) -> List[ApprovalRule]:
    """Parse a list of ``{allow: ..., deny: ...}`` YAML entries into :class:`ApprovalRule` objects."""
    rules: List[ApprovalRule] = []
    for i, entry in enumerate(rules_data):
        if not isinstance(entry, dict):
            raise ValueError(f"Rule entry {i} must be a dict, got {type(entry).__name__}")
        if "allow" in entry:
            tool, params = _parse_rule_string(entry["allow"])
            rules.append(ApprovalRule(action="allow", tool=tool, params=params))
        elif "deny" in entry:
            tool, params = _parse_rule_string(entry["deny"])
            rules.append(ApprovalRule(action="deny", tool=tool, params=params))
        else:
            raise ValueError(
                f"Rule entry {i} must have an 'allow' or 'deny' key: {entry!r}"
            )
    return rules


def _field_matches(pattern: str, value: Any) -> bool:
    value_str = str(value)
    if pattern.startswith("re:"):
        return bool(re.search(pattern[3:], value_str))
    return fnmatch.fnmatch(value_str, pattern)


def _rule_matches(rule: ApprovalRule, tool_name: str, params: Dict[str, Any]) -> bool:
    if rule.tool != tool_name:
        return False
    for field, pattern in rule.params.items():
        if field not in params:
            return False
        if not _field_matches(pattern, params[field]):
            return False
    return True


def render_call_string(tool_name: str, params: Dict[str, Any]) -> str:
    """Render ``tool_name({'key': 'value', ...})`` with sorted keys for stable display."""
    sorted_params = {k: params[k] for k in sorted(params)}
    return f"{tool_name}({sorted_params!r})"


class ApprovalRulesEngine:
    """Evaluates an ordered allow/deny rule list against tool calls."""

    def __init__(self, file_rules: Optional[List[ApprovalRule]] = None) -> None:
        self._file_rules: List[ApprovalRule] = file_rules or []
        self._session_rules: List[ApprovalRule] = []

    @classmethod
    def from_file(cls, path: Path) -> "ApprovalRulesEngine":
        """Load rules from a YAML file; returns an empty engine if the file is absent."""
        if not path.exists():
            logger.debug(f"Approvals file not found at {path}; no file rules loaded")
            return cls()
        try:
            with open(path, encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"Malformed YAML in {path}: {exc}") from exc

        rules = parse_rules(data.get("rules", []))
        logger.info(f"Loaded {len(rules)} approval rule(s) from {path}")
        return cls(file_rules=rules)

    def add_session_rule(self, rule: ApprovalRule) -> None:
        """Prepend a rule active only for the current session."""
        self._session_rules.insert(0, rule)

    def clear_session_rules(self) -> None:
        """Remove all session-scoped rules."""
        self._session_rules.clear()

    def evaluate(
        self, tool_name: str, params: Dict[str, Any]
    ) -> Optional[RuleAction]:
        """Return ``'allow'``, ``'deny'``, or ``None`` (no match — caller should prompt)."""
        for rule in self._session_rules + self._file_rules:
            if _rule_matches(rule, tool_name, params):
                logger.debug(
                    f"Rule matched: {rule.action} {rule.tool} for call "
                    f"{render_call_string(tool_name, params)}"
                )
                return rule.action
        return None
