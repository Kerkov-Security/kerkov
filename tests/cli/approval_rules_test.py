"""Tests for the approval rules engine."""

import tempfile
import unittest
from pathlib import Path

from src.cli.approval_rules import (
    ApprovalRule,
    ApprovalRulesEngine,
    parse_rules,
    render_call_string,
    _parse_rule_string,
    _rule_matches,
)


class ParseRuleStringTest(unittest.TestCase):
    def test_simple_glob(self):
        tool, params = _parse_rule_string("exec({'command': 'ping *'})")
        self.assertEqual(tool, "exec")
        self.assertEqual(params, {"command": "ping *"})

    def test_empty_params(self):
        tool, params = _parse_rule_string("read({})")
        self.assertEqual(tool, "read")
        self.assertEqual(params, {})

    def test_empty_parens(self):
        tool, params = _parse_rule_string("read()")
        self.assertEqual(tool, "read")
        self.assertEqual(params, {})

    def test_multiple_fields(self):
        tool, params = _parse_rule_string("http({'method': 'GET', 'url': 'https://*'})")
        self.assertEqual(tool, "http")
        self.assertEqual(params, {"method": "GET", "url": "https://*"})

    def test_regex_prefix_preserved(self):
        tool, params = _parse_rule_string("exec({'command': 're:.*sudo.*'})")
        self.assertEqual(params["command"], "re:.*sudo.*")

    def test_missing_paren_raises(self):
        with self.assertRaises(ValueError):
            _parse_rule_string("exec")

    def test_non_dict_raises(self):
        with self.assertRaises(ValueError):
            _parse_rule_string("exec(['ls'])")


class ParseRulesTest(unittest.TestCase):
    def test_allow_entry(self):
        rules = parse_rules([{"allow": "exec({'command': 'ls *'})"}])
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].action, "allow")
        self.assertEqual(rules[0].tool, "exec")
        self.assertEqual(rules[0].params, {"command": "ls *"})

    def test_deny_entry(self):
        rules = parse_rules([{"deny": "exec({'command': 'rm *'})"}])
        self.assertEqual(rules[0].action, "deny")

    def test_mixed_list(self):
        rules = parse_rules(
            [
                {"deny": "exec({'command': 'rm *'})"},
                {"allow": "exec({'command': 'ls *'})"},
            ]
        )
        self.assertEqual(rules[0].action, "deny")
        self.assertEqual(rules[1].action, "allow")

    def test_unknown_key_raises(self):
        with self.assertRaises(ValueError):
            parse_rules([{"noop": "exec({})"}])


class RuleMatchesTest(unittest.TestCase):
    def _rule(self, action: str, tool: str, **params: str) -> ApprovalRule:
        return ApprovalRule(action=action, tool=tool, params=params)  # type: ignore[arg-type]

    def test_tool_name_mismatch(self):
        rule = self._rule("allow", "read")
        self.assertFalse(_rule_matches(rule, "exec", {}))

    def test_empty_params_matches_any_call(self):
        rule = self._rule("allow", "read")
        self.assertTrue(_rule_matches(rule, "read", {"path": "/etc/passwd"}))

    def test_glob_match(self):
        rule = self._rule("deny", "exec", command="rm *")
        self.assertTrue(_rule_matches(rule, "exec", {"command": "rm foo"}))
        self.assertFalse(_rule_matches(rule, "exec", {"command": "ls foo"}))

    def test_glob_wildcard_any(self):
        rule = self._rule("allow", "exec", command="ping *")
        self.assertTrue(_rule_matches(rule, "exec", {"command": "ping google.com"}))

    def test_regex_match(self):
        rule = self._rule("deny", "exec", command="re:.*sudo.*")
        self.assertTrue(_rule_matches(rule, "exec", {"command": "sudo rm -rf /"}))
        self.assertFalse(_rule_matches(rule, "exec", {"command": "ls"}))

    def test_partial_match_ignores_extra_params(self):
        rule = self._rule("allow", "http", method="GET")
        self.assertTrue(
            _rule_matches(rule, "http", {"method": "GET", "url": "https://example.com"})
        )

    def test_missing_field_no_match(self):
        rule = self._rule("allow", "exec", command="ls *")
        self.assertFalse(_rule_matches(rule, "exec", {}))


class RenderCallStringTest(unittest.TestCase):
    def test_sorted_keys(self):
        result = render_call_string("exec", {"mode": "one_shot", "command": "ls"})
        self.assertEqual(result, "exec({'command': 'ls', 'mode': 'one_shot'})")

    def test_empty_params(self):
        result = render_call_string("read", {})
        self.assertEqual(result, "read({})")


class ApprovalRulesEngineTest(unittest.TestCase):
    def test_no_rules_returns_none(self):
        engine = ApprovalRulesEngine()
        result = engine.evaluate("exec", {"command": "ls"})
        self.assertIsNone(result)

    def test_allow_rule_matched(self):
        engine = ApprovalRulesEngine(
            file_rules=[ApprovalRule(action="allow", tool="read", params={})]
        )
        result = engine.evaluate("read", {"path": "/tmp/file"})
        self.assertEqual(result, "allow")

    def test_deny_rule_matched(self):
        engine = ApprovalRulesEngine(
            file_rules=[ApprovalRule(action="deny", tool="exec", params={"command": "rm *"})]
        )
        self.assertEqual(engine.evaluate("exec", {"command": "rm foo"}), "deny")

    def test_first_match_wins(self):
        engine = ApprovalRulesEngine(
            file_rules=[
                ApprovalRule(action="deny", tool="exec", params={"command": "rm *"}),
                ApprovalRule(action="allow", tool="exec", params={}),
            ]
        )
        self.assertEqual(engine.evaluate("exec", {"command": "rm foo"}), "deny")
        self.assertEqual(engine.evaluate("exec", {"command": "ls"}), "allow")

    def test_session_rules_checked_before_file_rules(self):
        engine = ApprovalRulesEngine(
            file_rules=[ApprovalRule(action="deny", tool="exec", params={"command": "ls *"})]
        )
        engine.add_session_rule(
            ApprovalRule(action="allow", tool="exec", params={"command": "ls *"})
        )
        self.assertEqual(engine.evaluate("exec", {"command": "ls /tmp"}), "allow")

    def test_clear_session_rules(self):
        engine = ApprovalRulesEngine()
        engine.add_session_rule(ApprovalRule(action="allow", tool="read", params={}))
        engine.clear_session_rules()
        self.assertIsNone(engine.evaluate("read", {}))

    def test_from_file_missing(self):
        engine = ApprovalRulesEngine.from_file(Path("/nonexistent/approvals.yaml"))
        self.assertIsNone(engine.evaluate("exec", {"command": "ls"}))

    def test_from_file_loads_rules(self):
        content = (
            "rules:\n"
            "  - allow: \"read({})\"\n"
            "  - deny: \"exec({'command': 'rm *'})\"\n"
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)

        try:
            engine = ApprovalRulesEngine.from_file(tmp_path)
            self.assertEqual(engine.evaluate("read", {"path": "/tmp/x"}), "allow")
            self.assertEqual(engine.evaluate("exec", {"command": "rm foo"}), "deny")
            self.assertIsNone(engine.evaluate("write", {"path": "/tmp/x", "content": ""}))
        finally:
            tmp_path.unlink()


if __name__ == "__main__":
    unittest.main()
