"""Tests for the approval rules engine."""

import tempfile
import unittest
from pathlib import Path

from kerkov.cli.approval_rules import (
    ApprovalRule,
    ApprovalRulesEngine,
    _parse_rule_string,
    _rule_matches,
    allow_rule_choices,
    append_rule_to_file,
    broad_allow_rule,
    default_approvals_source,
    exact_allow_rule,
    parse_rules,
    render_call_string,
)
from kerkov.core.run_workspace import reset_current_workspace, set_current_workspace
from kerkov.tools.exec_tool import ExecTool
from kerkov.tools.http_tool import HTTPTool
from kerkov.tools.process_tool import ProcessTool
from kerkov.tools.read_tool import ReadTool
from kerkov.tools.web_search_tool import WebSearchTool
from kerkov.tools.write_tool import WriteTool


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
        """Session rules outrank file rules — but never a file deny."""
        engine = ApprovalRulesEngine(
            file_rules=[
                ApprovalRule(action="allow", tool="exec", params={"command": "ls *"})
            ]
        )
        engine.add_session_rule(
            ApprovalRule(action="deny", tool="exec", params={"command": "ls /tmp"})
        )
        self.assertEqual(engine.evaluate("exec", {"command": "ls /tmp"}), "deny")
        self.assertEqual(engine.evaluate("exec", {"command": "ls /var"}), "allow")

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


class AllowRuleChoicesTest(unittest.TestCase):
    def test_http_broad_rung_scopes_to_origin_and_method(self):
        choices = allow_rule_choices(
            HTTPTool(),
            {
                "method": "GET",
                "timeout": 30,
                "url": "https://course.example.com/lectures.html",
            },
        )
        self.assertEqual(len(choices), 2)
        self.assertEqual(
            choices[0].rule.params,
            {"method": "GET", "url": "https://course.example.com/*"},
        )

    def test_http_origin_keeps_non_default_port(self):
        rule = broad_allow_rule(
            HTTPTool(), {"method": "GET", "url": "http://192.0.2.10:8080/admin/login"}
        )
        assert rule is not None
        self.assertEqual(rule.params["url"], "http://192.0.2.10:8080/*")

    def test_http_origin_drops_url_credentials(self):
        rule = broad_allow_rule(
            HTTPTool(), {"method": "GET", "url": "https://user:pw@example.org/a"}
        )
        assert rule is not None
        self.assertNotIn("pw", rule.params["url"])
        self.assertEqual(rule.params["url"], "https://example.org/*")

    def test_http_profile_call_scopes_to_profile(self):
        rule = broad_allow_rule(
            HTTPTool(), {"method": "POST", "profile": "htb", "path": "/api/x"}
        )
        assert rule is not None
        self.assertEqual(rule.params, {"profile": "htb", "method": "POST"})

    def test_http_without_url_or_profile_has_no_broad_rung(self):
        self.assertIsNone(broad_allow_rule(HTTPTool(), {"method": "GET"}))

    def test_exec_is_exact_only(self):
        self.assertIsNone(broad_allow_rule(ExecTool(), {"command": "sudo -l"}))
        choices = allow_rule_choices(ExecTool(), {"command": "sudo -l"})
        self.assertEqual(len(choices), 1)
        self.assertEqual(choices[0].rule.params, {"command": "sudo -l"})

    def test_path_tools_scope_to_parent_directory(self):
        rule = broad_allow_rule(ReadTool(), {"file_path": "/etc/passwd"})
        assert rule is not None
        self.assertEqual(rule.params, {"file_path": "/etc/*"})

    def test_bare_filename_has_no_directory_boundary(self):
        self.assertIsNone(broad_allow_rule(ReadTool(), {"file_path": "passwd"}))

    def test_action_tools_scope_to_action(self):
        rule = broad_allow_rule(ProcessTool(), {"action": "kill", "session_id": "s3"})
        assert rule is not None
        self.assertEqual(rule.params, {"action": "kill"})

    def test_tool_without_boundary_offers_blanket(self):
        rule = broad_allow_rule(WebSearchTool(), {"query": "cve-2024-1234"})
        assert rule is not None
        self.assertEqual(rule.params, {})

    def test_volatile_params_dropped_from_exact_rule(self):
        rule = exact_allow_rule(
            HTTPTool(), {"method": "GET", "url": "https://example.net/a", "timeout": 30}
        )
        self.assertEqual(rule.params, {"method": "GET", "url": "https://example.net/a"})

    def test_write_content_never_reaches_a_rule(self):
        params = {"file_path": "/tmp/a.env", "content": "TOKEN=hunter2\n"}
        for rule_string in (
            c.rule_string for c in allow_rule_choices(WriteTool(), params)
        ):
            self.assertNotIn("hunter2", rule_string)

    def test_url_credentials_never_reach_the_exact_rule(self):
        """The URL identifies the call, so it is sanitized rather than dropped."""
        params = {"method": "GET", "url": "https://user:pw@example.org/admin?x=1"}
        for choice in allow_rule_choices(HTTPTool(), params):
            self.assertNotIn("pw", choice.rule_string)
            self.assertNotIn("user", choice.rule_string)
        exact = exact_allow_rule(HTTPTool(), params)
        self.assertEqual(exact.params["url"], "https://example.org/admin[?]x=1")

    def test_sanitized_exact_url_still_matches_the_call_it_came_from(self):
        engine = ApprovalRulesEngine()
        params = {"method": "GET", "url": "https://user:pw@example.org/admin"}
        engine.add_workspace_rule(exact_allow_rule(HTTPTool(), params))
        self.assertEqual(
            engine.evaluate("http", params),
            "allow",
        )

    def test_request_bodies_never_reach_a_rule(self):
        params = {
            "method": "POST",
            "url": "https://example.org/login",
            "json": {"password": "hunter2"},
            "data": "token=hunter2",
        }
        for choice in allow_rule_choices(HTTPTool(), params):
            self.assertNotIn("hunter2", choice.rule_string)

    def test_http_headers_never_reach_a_rule(self):
        params = {
            "method": "GET",
            "url": "https://example.net/a",
            "headers": {"Authorization": "Bearer tok"},
        }
        for rule_string in (
            c.rule_string for c in allow_rule_choices(HTTPTool(), params)
        ):
            self.assertNotIn("tok", rule_string)

    def test_glob_metacharacters_in_value_are_escaped(self):
        rule = exact_allow_rule(ReadTool(), {"file_path": "/tmp/a[1]*.txt"})
        self.assertTrue(_rule_matches(rule, "read", {"file_path": "/tmp/a[1]*.txt"}))
        self.assertFalse(_rule_matches(rule, "read", {"file_path": "/tmp/a1x.txt"}))

    def test_broad_rung_omitted_when_identical_to_exact(self):
        choices = allow_rule_choices(ProcessTool(), {"action": "list"})
        self.assertEqual(len(choices), 1)
        self.assertEqual(choices[0].label, "this exact call")

    def test_exact_rung_is_always_last(self):
        for tool, params in (
            (HTTPTool(), {"method": "GET", "url": "https://example.net/a"}),
            (ReadTool(), {"file_path": "/etc/passwd"}),
            (ExecTool(), {"command": "ls"}),
        ):
            self.assertEqual(
                allow_rule_choices(tool, params)[-1].label, "this exact call"
            )


class BroadRuleBoundaryTest(unittest.TestCase):
    """The origin rung must not leak past the host it was granted for."""

    def setUp(self):
        self.engine = ApprovalRulesEngine()
        rule = broad_allow_rule(
            HTTPTool(), {"method": "GET", "url": "https://example.com/lectures.html"}
        )
        assert rule is not None
        self.engine.add_workspace_rule(rule)

    def _evaluate(self, **params):
        return self.engine.evaluate("http", params)

    def test_sibling_path_is_allowed(self):
        self.assertEqual(
            self._evaluate(method="GET", url="https://example.com/notes.html"), "allow"
        )

    def test_timeout_variation_does_not_break_the_match(self):
        self.assertEqual(
            self._evaluate(method="GET", url="https://example.com/a", timeout=60),
            "allow",
        )
        self.assertEqual(
            self._evaluate(method="GET", url="https://example.com/a"), "allow"
        )

    def test_other_method_still_prompts(self):
        self.assertIsNone(self._evaluate(method="POST", url="https://example.com/a"))

    def test_other_host_still_prompts(self):
        self.assertIsNone(
            self._evaluate(method="GET", url="https://elsewhere.example/a")
        )

    def test_suffix_lookalike_host_still_prompts(self):
        self.assertIsNone(
            self._evaluate(method="GET", url="https://example.com.attacker.example/a")
        )

    def test_other_scheme_still_prompts(self):
        self.assertIsNone(self._evaluate(method="GET", url="http://example.com/a"))

    def test_other_port_still_prompts(self):
        self.assertIsNone(
            self._evaluate(method="GET", url="https://example.com:8443/a")
        )


class SavedRuleBoundaryTest(unittest.TestCase):
    def test_directory_rule_round_trip_blocks_traversal_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            allowed = root / "reports"
            allowed.mkdir()
            outside = root / "private"
            outside.mkdir()
            (allowed / "link").symlink_to(outside, target_is_directory=True)
            workspace_token = set_current_workspace(root)
            self.addCleanup(reset_current_workspace, workspace_token)
            for tool in (ReadTool(), WriteTool()):
                with self.subTest(tool=tool.name):
                    rule = broad_allow_rule(tool, {"file_path": "reports/first.txt"})
                    assert rule is not None
                    rules_file = root / f"{tool.name}-approvals.yaml"
                    append_rule_to_file(rules_file, rule)
                    engine = ApprovalRulesEngine.from_file(rules_file)
                    for target in ("reports/next.txt", str(allowed / "next.txt")):
                        self.assertEqual(
                            engine.evaluate(tool.name, {"file_path": target}), "allow"
                        )
                    for target in (
                        "reports/../private/secret.txt",
                        str(allowed / ".." / "private" / "secret.txt"),
                        "reports/link/secret.txt",
                        str(allowed / "link" / "secret.txt"),
                    ):
                        self.assertIsNone(
                            engine.evaluate(tool.name, {"file_path": target}), target
                        )

    def test_exact_file_rule_does_not_follow_a_retargeted_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            link = root / "link.txt"
            link.symlink_to(root / "first.txt")
            params = {"file_path": str(link)}
            rule = exact_allow_rule(WriteTool(), params)
            assert rule is not None
            engine = ApprovalRulesEngine(file_rules=[rule])
            self.assertEqual(engine.evaluate("write", params), "allow")
            link.unlink()
            link.symlink_to(root / "second.txt")
            self.assertIsNone(engine.evaluate("write", params))

    def test_deny_still_matches_original_path_and_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "link").symlink_to(root / "target")
            for tool, field, value in (
                ("write", "file_path", str(root / "link")),
                ("http", "url", "https://user:pw@example.org/a"),
            ):
                rule = ApprovalRule(action="deny", tool=tool, params={field: value})
                self.assertTrue(_rule_matches(rule, tool, {field: value}))

    def test_http_rules_round_trip_with_credentials_and_ipv6(self):
        with tempfile.TemporaryDirectory() as tmp:
            for host in ("example.org", "[2001:db8::1]", "[2001:db8::1]:8443"):
                params = {"method": "POST", "url": f"https://user:pw@{host}/a?x=1"}
                for index, choice in enumerate(allow_rule_choices(HTTPTool(), params)):
                    with self.subTest(host=host, label=choice.label):
                        path = Path(tmp) / f"{host.replace(':', '_')}-{index}.yaml"
                        append_rule_to_file(path, choice.rule)
                        self.assertNotIn("user:pw", path.read_text())
                        engine = ApprovalRulesEngine.from_file(path)
                        self.assertEqual(engine.evaluate("http", params), "allow")
                        self.assertIsNone(
                            engine.evaluate("http", {**params, "method": "GET"})
                        )
                        self.assertIsNone(
                            engine.evaluate(
                                "http",
                                {**params, "url": "https://elsewhere.example/a?x=1"},
                            )
                        )
                        sibling = {**params, "url": f"https://user:pw@{host}/b"}
                        self.assertEqual(
                            engine.evaluate("http", sibling),
                            "allow" if index == 0 else None,
                        )

    def test_invalid_urls_offer_no_saved_grant(self):
        for url in (
            "https://user:pw@example.org:invalid/a",
            "https://example.org:70000/a",
            "https://[broken]/a",
            "https:///missing-host",
        ):
            with self.subTest(url=url):
                params = {"method": "POST", "url": url}
                self.assertEqual(allow_rule_choices(HTTPTool(), params), [])
                self.assertIsNone(exact_allow_rule(HTTPTool(), params))
                self.assertIsNone(broad_allow_rule(HTTPTool(), params))

    def test_packaged_and_source_defaults_require_http_approval(self):
        root = Path(__file__).resolve().parents[2]
        source = ApprovalRulesEngine.from_file(root / "config/approvals.yaml")
        packaged = ApprovalRulesEngine.from_file(
            root / "src/kerkov/config/approvals.yaml"
        )
        self.assertEqual(source.file_rules, packaged.file_rules)
        for engine in (source, packaged):
            self.assertIsNone(
                engine.evaluate(
                    "http", {"method": "GET", "url": "https://unapproved.example/"}
                )
            )


class DenyPrecedenceTest(unittest.TestCase):
    """A deny in any tier outranks an allow in any tier."""

    def setUp(self):
        self.engine = ApprovalRulesEngine(
            file_rules=parse_rules(
                [{"deny": "exec({'command': 're:(^|[^a-z])sudo($|[^a-z])'})"}]
            )
        )

    def test_blanket_workspace_allow_cannot_retire_a_file_deny(self):
        self.engine.add_workspace_rule(
            ApprovalRule(action="allow", tool="exec", params={})
        )
        self.assertEqual(self.engine.evaluate("exec", {"command": "sudo -l"}), "deny")

    def test_session_allow_cannot_retire_a_file_deny(self):
        self.engine.add_session_rule(
            ApprovalRule(action="allow", tool="exec", params={"command": "sudo *"})
        )
        self.assertEqual(self.engine.evaluate("exec", {"command": "sudo -l"}), "deny")

    def test_unrelated_calls_still_allowed(self):
        self.engine.add_workspace_rule(
            ApprovalRule(action="allow", tool="exec", params={})
        )
        self.assertEqual(self.engine.evaluate("exec", {"command": "ls -la"}), "allow")

    def test_no_match_still_returns_none(self):
        self.assertIsNone(self.engine.evaluate("http", {"method": "GET"}))

    def test_hand_written_file_allow_still_overrides_a_later_file_deny(self):
        """The floor is under saved rules only; files keep their own order."""
        engine = ApprovalRulesEngine(
            file_rules=parse_rules(
                [{"allow": "exec({'command': 'sudo -l'})"}, {"deny": "exec({})"}]
            )
        )
        self.assertEqual(engine.evaluate("exec", {"command": "sudo -l"}), "allow")

    def test_saved_deny_still_outranks_a_file_allow(self):
        engine = ApprovalRulesEngine(file_rules=parse_rules([{"allow": "exec({})"}]))
        engine.add_workspace_rule(
            ApprovalRule(action="deny", tool="exec", params={"command": "curl *"})
        )
        self.assertEqual(engine.evaluate("exec", {"command": "curl x"}), "deny")
        self.assertEqual(engine.evaluate("exec", {"command": "ls"}), "allow")
