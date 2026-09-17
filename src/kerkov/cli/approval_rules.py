"""
Pattern-based tool approval rules engine.

Rules are evaluated in order; first match wins, except that a deny from a
rules file outranks any allow saved from the approval prompt. If no rule
matches the caller decides what to do (typically: prompt the user).

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
import os
import re
from importlib import resources
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Literal,
    Optional,
    Tuple,
)

import yaml
from pydantic import BaseModel, TypeAdapter, ValidationError

from kerkov.core.approval_scope import escape_glob_literal, split_approval_url
from kerkov.core.logging.logger_factory import get_logger
from kerkov.core.run_workspace import get_current_workspace, resolve_file_path

if TYPE_CHECKING:  # importing kerkov.tools eagerly would pull in the whole suite
    from kerkov.tools.base_tool import BaseTool

logger = get_logger("ApprovalRules")

RuleAction = Literal["allow", "deny"]
_WRITE_APPEND_ADAPTER: TypeAdapter[Optional[bool]] = TypeAdapter(Optional[bool])

DEFAULT_APPROVALS_PATH = "config/approvals.yaml"
DEFAULT_APPROVALS_PACKAGE = "kerkov.config"
DEFAULT_APPROVALS_RESOURCE = "approvals.yaml"


def default_approvals_source(start_dir: Optional[Path] = None) -> Any:
    """Return the source-checkout approvals file, or the packaged default."""
    project_root = _find_project_root(start_dir or Path.cwd())
    if project_root is not None:
        repo_path = project_root / DEFAULT_APPROVALS_PATH
        if repo_path.exists():
            return repo_path
    return resources.files(DEFAULT_APPROVALS_PACKAGE).joinpath(
        DEFAULT_APPROVALS_RESOURCE
    )


def _find_project_root(start_dir: Path) -> Optional[Path]:
    current = start_dir.resolve()
    if current.is_file():
        current = current.parent

    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return None


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
            raise ValueError(
                f"Rule entry {i} must be a dict, got {type(entry).__name__}"
            )
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
        value = params[field]
        candidates = [str(value)]
        try:
            if tool_name in {"read", "write"} and field == "file_path":
                target = resolve_file_path(str(value))
                candidates = [target.as_posix()]
                root = get_current_workspace() or Path.cwd()
                # Keep relative hand-written rules relative to the workspace.
                try:
                    candidates.append(Path(os.path.relpath(target, root)).as_posix())
                except ValueError:  # A different drive on Windows.
                    pass
            elif tool_name == "http" and field == "url":
                candidates = [split_approval_url(str(value)).geturl()]
        except (OSError, RuntimeError, ValueError):
            candidates = []
        # Existing denies may refer to a symlink spelling or URL userinfo.
        if rule.action == "deny":
            candidates.append(str(value))
        if not any(_field_matches(pattern, candidate) for candidate in candidates):
            return False
    return True


def _escape_display_controls(value: str) -> str:
    """Escape characters that can alter terminal or browser display layout."""
    return "".join(
        character if character.isprintable() else ascii(character)[1:-1]
        for character in value
    )


def split_write_content_lines(content: str) -> List[str]:
    """Split on LF only, preserving other controls for display escaping."""
    lines = content.split("\n")
    if not lines[-1]:
        lines.pop()
    return lines


def render_call_string(tool_name: str, params: Dict[str, Any]) -> str:
    """Render a stable tool call summary, omitting write payloads."""
    if tool_name == "write":
        file_path = str(params.get("file_path", "<unknown>"))
        display_path = _escape_display_controls(file_path)
        file_type = Path(file_path).suffix.lstrip(".").lower() or "text"
        display_file_type = _escape_display_controls(file_type)
        content = str(params.get("content", ""))
        line_count = len(split_write_content_lines(content))
        line_label = "line" if line_count == 1 else "lines"
        try:
            append = _WRITE_APPEND_ADAPTER.validate_python(params.get("append"))
            action = "append" if append else "write"
        except ValidationError:
            action = "write (invalid append)"
        return (
            f"{action} → {display_path} "
            f"({display_file_type}, {line_count} {line_label})"
        )

    sorted_params = {k: params[k] for k in sorted(params)}
    return f"{tool_name}({sorted_params!r})"


class AllowRuleChoice(BaseModel):
    """One selectable breadth for a saved allow rule, with its literal text."""

    label: str
    rule: ApprovalRule

    @property
    def rule_string(self) -> str:
        return _format_rule_string(self.rule.tool, self.rule.params)


def exact_allow_rule(
    tool: "BaseTool", params: Dict[str, Any]
) -> Optional[ApprovalRule]:
    """Build an allow rule matching only this call, minus its volatile params.

    A tool may also substitute a value via ``exact_overrides`` — used for a
    parameter that identifies the call but carries a secret inside it.
    """
    scope = tool.approval_scope(params)
    if not scope.saveable:
        return None
    return ApprovalRule(
        action="allow",
        tool=tool.name,
        params={
            k: scope.exact_overrides.get(k, escape_glob_literal(v))
            for k, v in params.items()
            if k not in scope.volatile_params
        },
    )


def broad_allow_rule(
    tool: "BaseTool", params: Dict[str, Any]
) -> Optional[ApprovalRule]:
    """Build the widest allow rule the tool offers for this call, or None."""
    scope = tool.approval_scope(params)
    broad_params = scope.broad_params
    if not scope.saveable or broad_params is None:
        return None
    return ApprovalRule(action="allow", tool=tool.name, params=broad_params)


def allow_rule_choices(
    tool: "BaseTool", params: Dict[str, Any]
) -> List[AllowRuleChoice]:
    """Return the saveable allow rules for a call, broadest first.

    The exact rule is always last; invalid targets offer no saved rules.
    The broad rule is dropped when identical to it, so the menu never offers
    the same grant twice.
    """
    choices: List[AllowRuleChoice] = []
    exact = exact_allow_rule(tool, params)
    if exact is None:
        return choices

    broad = broad_allow_rule(tool, params)
    if broad is not None and broad.params != exact.params:
        choices.append(
            AllowRuleChoice(
                label=(
                    f"any {tool.name} call"
                    if not broad.params
                    else f"any matching {tool.name} call"
                ),
                rule=broad,
            )
        )

    choices.append(AllowRuleChoice(label="this exact call", rule=exact))
    return choices


class ApprovalRulesEngine:
    """Evaluates an ordered allow/deny rule list against tool calls.

    Evaluation order (first match wins), after any matching file-tier deny:
      1. workspace rules – per-engagement rules; highest priority
      2. session rules   – approved interactively this run (no active workspace)
      3. file rules      – user-level then project-level, merged in priority order
    """

    def __init__(self, file_rules: Optional[List[ApprovalRule]] = None) -> None:
        self._file_rules: List[ApprovalRule] = file_rules or []
        self._session_rules: List[ApprovalRule] = []
        self._workspace_rules: List[ApprovalRule] = []

    @classmethod
    def from_file(cls, path: Any) -> "ApprovalRulesEngine":
        """Load rules from a YAML file; returns an empty engine if the file is absent."""
        if not _approval_source_exists(path):
            logger.debug(f"Approvals file not found at {path}; no file rules loaded")
            return cls()
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"Malformed YAML in {path}: {exc}") from exc

        rules = parse_rules(data.get("rules", []))
        logger.info(f"Loaded {len(rules)} approval rule(s) from {path}")
        return cls(file_rules=rules)

    @classmethod
    def from_files(cls, *paths: Any) -> "ApprovalRulesEngine":
        """Load and merge rules from multiple files in priority order.

        Rules from earlier paths take precedence over later ones (first-match-wins).
        Missing files are silently skipped.
        """
        merged: List[ApprovalRule] = []
        for path in paths:
            if not _approval_source_exists(path):
                logger.debug(f"Approvals file not found at {path}; skipping")
                continue
            try:
                with path.open("r", encoding="utf-8") as fh:
                    data = yaml.safe_load(fh) or {}
            except yaml.YAMLError as exc:
                raise ValueError(f"Malformed YAML in {path}: {exc}") from exc
            rules = parse_rules(data.get("rules", []))
            logger.info(f"Loaded {len(rules)} approval rule(s) from {path}")
            merged.extend(rules)
        return cls(file_rules=merged)

    def add_session_rule(self, rule: ApprovalRule) -> None:
        """Prepend a rule active only for the current session."""
        self._session_rules.insert(0, rule)

    def clear_session_rules(self) -> None:
        """Remove all session-scoped rules."""
        self._session_rules.clear()

    @property
    def file_rules(self) -> List[ApprovalRule]:
        """Return a defensive copy of the configured file-backed rules."""

        return list(self._file_rules)

    def set_workspace_rules(self, rules: List[ApprovalRule]) -> None:
        """Replace the workspace-tier rules."""
        self._workspace_rules = list(rules)

    def add_workspace_rule(self, rule: ApprovalRule) -> None:
        """Prepend a rule to the workspace tier (also used for interactive approvals when a workspace is active)."""
        self._workspace_rules.insert(0, rule)

    def evaluate(self, tool_name: str, params: Dict[str, Any]) -> Optional[RuleAction]:
        """Return ``'allow'``, ``'deny'``, or ``None`` (no match — caller should prompt).

        A file-tier deny is a floor under the interactive tiers: tier order alone
        would let a rule saved from the approval prompt retire the shipped denies
        for ``rm -rf``, ``sudo`` and ``mkfs``, which is the one grant a keystroke
        must not be able to make. Files keep their own precedence among
        themselves, so a hand-written user rule can still override a project
        deny — that is a deliberate edit, not a keystroke.
        """
        file_verdict = self._first_match(self._file_rules, tool_name, params)
        if file_verdict == "deny":
            return "deny"

        saved_verdict = self._first_match(
            self._workspace_rules + self._session_rules, tool_name, params
        )
        return saved_verdict if saved_verdict is not None else file_verdict

    @staticmethod
    def _first_match(
        rules: List[ApprovalRule], tool_name: str, params: Dict[str, Any]
    ) -> Optional[RuleAction]:
        for rule in rules:
            if _rule_matches(rule, tool_name, params):
                logger.debug(
                    f"Rule matched: {rule.action} {rule.tool} for call "
                    f"{render_call_string(tool_name, params)}"
                )
                return rule.action
        return None


def _approval_source_exists(source: Any) -> bool:
    if hasattr(source, "exists"):
        return bool(source.exists())
    if hasattr(source, "is_file"):
        return bool(source.is_file())
    return False


def _format_rule_string(tool: str, params: Dict[str, str]) -> str:
    """Format a rule into ``tool({'key': 'value'})`` notation for YAML storage."""
    sorted_params = {k: params[k] for k in sorted(params)}
    return f"{tool}({sorted_params!r})"


def append_rule_to_file(path: Path, rule: ApprovalRule) -> None:
    """Append a single rule to a YAML approvals file, creating it if absent."""
    data: Dict[str, Any] = {}
    if path.exists():
        try:
            with open(path, encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"Malformed YAML in {path}: {exc}") from exc

    rules: List[Any] = data.get("rules", [])
    rules.append({rule.action: _format_rule_string(rule.tool, rule.params)})
    data["rules"] = rules

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        yaml.dump(data, fh, default_flow_style=False)
    logger.debug(f"Appended {rule.action} rule for '{rule.tool}' to {path}")
