"""Tests for narration detection in exec commands."""

import unittest

from kerkov.tools.command_narration import find_narration_comments, narration_nudge


class FindNarrationCommentsTest(unittest.TestCase):
    def test_leading_narration_line(self):
        found = find_narration_comments(
            "# first, let's see what's listening\nnmap -sV -p- 10.0.0.1"
        )
        self.assertEqual(found, ["first, let's see what's listening"])

    def test_multiple_narration_lines(self):
        found = find_narration_comments(
            "# Step 1: enumerate\n"
            "nmap -p- 10.0.0.1\n"
            "# now let's grab the banner\n"
            "curl -sI http://10.0.0.1"
        )
        self.assertEqual(found, ["Step 1: enumerate", "now let's grab the banner"])

    def test_indented_comment_counts(self):
        found = find_narration_comments("  # now check it\nls")
        self.assertEqual(found, ["now check it"])

    def test_no_comment(self):
        self.assertEqual(find_narration_comments("nmap -sV 10.0.0.1"), [])

    def test_empty_comment_is_ignored(self):
        self.assertEqual(find_narration_comments("#\nls"), [])

    def test_detection_does_not_depend_on_phrasing(self):
        # No opener list would have caught these shapes.
        for command in (
            "# Kick things off by mapping the subnet\nls",
            "# ports\nnmap 10.0.0.1",
            "# d'abord, scanner le réseau\nnmap 10.0.0.1",
        ):
            with self.subTest(command=command):
                self.assertTrue(find_narration_comments(command))


class ToolDirectiveTest(unittest.TestCase):
    """Comments aimed at another tool are not the model narrating."""

    def _assert_quiet(self, command):
        self.assertEqual(find_narration_comments(command), [])
        self.assertIsNone(narration_nudge(command))

    def test_shebang(self):
        self._assert_quiet("#!/bin/bash\nls -la")

    def test_linter_directives(self):
        self._assert_quiet("# nosec\nnmap -sV 10.0.0.1")
        self._assert_quiet("# noqa: E501\npython x.py")
        self._assert_quiet("# shellcheck disable=SC2086\ngrep x f")

    def test_prose_still_counts(self):
        self.assertEqual(
            find_narration_comments("# TODO: retry on failure\ncmd"),
            ["TODO: retry on failure"],
        )


class NudgeTest(unittest.TestCase):
    def test_nudge_mentions_count_and_first_comment(self):
        nudge = narration_nudge("# I'll check the port\nss -tlnp")
        self.assertIsNotNone(nudge)
        self.assertIn("1 narration comment", nudge)
        self.assertIn("I'll check the port", nudge)

    def test_nudge_counts_extras(self):
        nudge = narration_nudge("# first, scan\nnmap 10.0.0.1\n# then read\ncat f")
        self.assertIn("2 narration comments", nudge)
        self.assertIn("+1 more", nudge)

    def test_long_comment_is_truncated(self):
        nudge = narration_nudge("# " + "x" * 200 + "\nls")
        self.assertIn("...", nudge)
        self.assertLess(len(nudge), 300)

    def test_no_nudge_without_narration(self):
        self.assertIsNone(narration_nudge("ls -la"))


class DetectionIsAdvisoryTest(unittest.TestCase):
    """The detector reads; it never rewrites. These are the cases that made
    stripping risky, and none of them can go wrong now."""

    def test_heredoc_body_is_reported_but_harmless(self):
        # Reporting a `#` line inside heredoc data costs a note, not a command.
        command = "cat <<'EOF'\n# now this is data\nEOF"
        self.assertEqual(find_narration_comments(command), ["now this is data"])

    def test_trailing_comments_are_not_detected(self):
        # Skipping these avoids having to know whether the `#` is quoted.
        self.assertEqual(find_narration_comments("nmap -sV 10.0.0.1  # I'll scan"), [])
        self.assertEqual(find_narration_comments("curl 'http://t/page#frag'"), [])


if __name__ == "__main__":
    unittest.main()
