"""Tests for the approval rules engine."""

import tempfile
import unittest
from pathlib import Path

from kerkov.cli.approval_rules import (
    ApprovalRule,
    ApprovalRulesEngine,
    _parse_rule_string,
    _rule_matches,
    append_rule_to_file,
    default_approvals_source,
    parse_rules,
    render_call_string,
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

    def test_write_content_is_summarized(self):
        result = render_call_string(
            "write",
            {
                "file_path": "scripts/check.py",
                "content": "print('first')\nprint('second')\n",
            },
        )

        self.assertEqual(result, "write → scripts/check.py (py, 2 lines)")
        self.assertNotIn("print", result)

    def test_write_append_action_is_visible(self):
        result = render_call_string(
            "write",
            {"file_path": "notes.txt", "content": "next", "append": "yes"},
        )

        self.assertEqual(result, "append → notes.txt (txt, 1 line)")

    def test_write_false_string_uses_overwrite_action(self):
        result = render_call_string(
            "write",
            {"file_path": "notes.txt", "content": "next", "append": "false"},
        )

        self.assertEqual(result, "write → notes.txt (txt, 1 line)")

    def test_write_append_action_survives_unrelated_invalid_fields(self):
        for append in (True, "yes", 1):
            for extra in ({}, {"content": None}, {"content": ["invalid"]}):
                with self.subTest(append=append, extra=extra):
                    result = render_call_string(
                        "write", {"file_path": "notes.txt", "append": append, **extra}
                    )
                    self.assertTrue(result.startswith("append → notes.txt"))

    def test_write_invalid_append_is_disclosed(self):
        result = render_call_string(
            "write",
            {"file_path": "notes.txt", "content": "next", "append": "invalid"},
        )

        self.assertEqual(result, "write (invalid append) → notes.txt (txt, 1 line)")

    def test_write_line_counts_use_only_lf(self):
        for content, expected in (
            ("", "0 lines"),
            ("\n", "1 line"),
            ("first\n", "1 line"),
            ("first\n\n", "2 lines"),
            ("first\nsecond", "2 lines"),
            ("first\r\nsecond\r\n", "2 lines"),
            ("first\v\f\r\x1c\x1d\x1e\x85\u2028\u2029second", "1 line"),
        ):
            with self.subTest(content=content):
                result = render_call_string(
                    "write", {"file_path": "notes.txt", "content": content}
                )
                self.assertEqual(result, f"write → notes.txt (txt, {expected})")

    def test_write_path_control_characters_are_escaped(self):
        result = render_call_string(
            "write",
            {
                "file_path": "scripts/\u202eevil\nname.py\u2066",
                "content": "print('safe')",
            },
        )

        self.assertEqual(
            result,
            "write → scripts/\\u202eevil\\nname.py\\u2066 " "(py\\u2066, 1 line)",
        )
        self.assertNotIn("\n", result)
        self.assertNotIn("\u202e", result)
        self.assertNotIn("\u2066", result)

    def test_write_path_preserves_printable_unicode(self):
        result = render_call_string(
            "write",
            {"file_path": "scripts/résumé-🐍.py", "content": "print('safe')"},
        )

        self.assertEqual(result, "write → scripts/résumé-🐍.py (py, 1 line)")


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
            file_rules=[
                ApprovalRule(action="deny", tool="exec", params={"command": "rm *"})
            ]
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
            file_rules=[
                ApprovalRule(action="deny", tool="exec", params={"command": "ls *"})
            ]
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
            '  - allow: "read({})"\n'
            "  - deny: \"exec({'command': 'rm *'})\"\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)

        try:
            engine = ApprovalRulesEngine.from_file(tmp_path)
            self.assertEqual(engine.evaluate("read", {"path": "/tmp/x"}), "allow")
            self.assertEqual(engine.evaluate("exec", {"command": "rm foo"}), "deny")
            self.assertIsNone(
                engine.evaluate("write", {"path": "/tmp/x", "content": ""})
            )
        finally:
            tmp_path.unlink()


class FromFilesTest(unittest.TestCase):
    def test_single_file_loaded(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write('rules:\n  - allow: "read({})"\n')
            path = Path(f.name)
        try:
            engine = ApprovalRulesEngine.from_files(path)
            self.assertEqual(engine.evaluate("read", {}), "allow")
        finally:
            path.unlink()

    def test_missing_files_skipped(self):
        engine = ApprovalRulesEngine.from_files(
            Path("/nonexistent/user.yaml"),
            Path("/nonexistent/project.yaml"),
        )
        self.assertIsNone(engine.evaluate("read", {}))

    def test_earlier_file_takes_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            user_path = Path(tmp) / "user.yaml"
            project_path = Path(tmp) / "project.yaml"
            user_path.write_text('rules:\n  - allow: "exec({})"\n', encoding="utf-8")
            project_path.write_text('rules:\n  - deny: "exec({})"\n', encoding="utf-8")
            engine = ApprovalRulesEngine.from_files(user_path, project_path)
            self.assertEqual(engine.evaluate("exec", {"command": "ls"}), "allow")

    def test_later_file_rules_apply_when_no_earlier_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            user_path = Path(tmp) / "user.yaml"
            project_path = Path(tmp) / "project.yaml"
            user_path.write_text('rules:\n  - allow: "read({})"\n', encoding="utf-8")
            project_path.write_text('rules:\n  - deny: "exec({})"\n', encoding="utf-8")
            engine = ApprovalRulesEngine.from_files(user_path, project_path)
            self.assertEqual(engine.evaluate("read", {}), "allow")
            self.assertEqual(engine.evaluate("exec", {"command": "rm foo"}), "deny")

    def test_partial_missing_still_loads_present_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_path = Path(tmp) / "project.yaml"
            project_path.write_text('rules:\n  - deny: "exec({})"\n', encoding="utf-8")
            engine = ApprovalRulesEngine.from_files(
                Path(tmp) / "user.yaml",  # missing
                project_path,
            )
            self.assertEqual(engine.evaluate("exec", {"command": "ls"}), "deny")

    def test_source_checkout_default_uses_repo_config(self):
        repo_root = Path(__file__).resolve().parents[2]
        source = default_approvals_source(start_dir=Path(__file__))
        self.assertEqual(source, repo_root / "config" / "approvals.yaml")

    def test_packaged_default_rules_load_without_repo_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = default_approvals_source(start_dir=Path(tmp))
            engine = ApprovalRulesEngine.from_files(source)

        self.assertEqual(engine.evaluate("exec", {"command": "sudo whoami"}), "deny")
        self.assertEqual(engine.evaluate("read", {"file_path": "/tmp/x"}), "allow")

    def test_zap_read_only_actions_are_allowed_but_scans_require_decision(self):
        source = default_approvals_source(start_dir=Path(__file__))
        engine = ApprovalRulesEngine.from_files(source)

        for action in ["status", "spider_status", "active_scan_status", "alerts"]:
            with self.subTest(action=action):
                self.assertEqual(engine.evaluate("zap", {"action": action}), "allow")
        self.assertIsNone(
            engine.evaluate(
                "zap",
                {"action": "active_scan_start", "target": "https://example.test"},
            )
        )
        self.assertIsNone(
            engine.evaluate("zap", {"action": "scan_stop", "scan_id": "1"})
        )


class AppendRuleToFileTest(unittest.TestCase):
    def _allow_rule(self, tool: str, **params: str) -> ApprovalRule:
        return ApprovalRule(action="allow", tool=tool, params=params)

    def _deny_rule(self, tool: str, **params: str) -> ApprovalRule:
        return ApprovalRule(action="deny", tool=tool, params=params)

    def test_creates_file_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "approvals.yaml"
            rule = self._allow_rule("read")
            append_rule_to_file(path, rule)
            self.assertTrue(path.exists())
            engine = ApprovalRulesEngine.from_file(path)
            self.assertEqual(engine.evaluate("read", {}), "allow")

    def test_appends_to_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "approvals.yaml"
            append_rule_to_file(path, self._allow_rule("read"))
            append_rule_to_file(path, self._deny_rule("exec", command="rm *"))
            engine = ApprovalRulesEngine.from_file(path)
            self.assertEqual(engine.evaluate("read", {}), "allow")
            self.assertEqual(engine.evaluate("exec", {"command": "rm foo"}), "deny")

    def test_round_trip_params(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "approvals.yaml"
            rule = self._allow_rule("exec", command="ping *", mode="one_shot")
            append_rule_to_file(path, rule)
            engine = ApprovalRulesEngine.from_file(path)
            self.assertEqual(
                engine.evaluate(
                    "exec", {"command": "ping 8.8.8.8", "mode": "one_shot"}
                ),
                "allow",
            )
            self.assertIsNone(
                engine.evaluate("exec", {"command": "ls", "mode": "one_shot"})
            )

    def test_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "dir" / "approvals.yaml"
            append_rule_to_file(path, self._allow_rule("read"))
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
