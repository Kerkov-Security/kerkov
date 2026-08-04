"""Tests for narration-comment stripping in exec commands."""

import unittest

from kerkov.tools.command_narration import strip_narration_comments


class StripNarrationCommentsTest(unittest.TestCase):
    def test_leading_narration_line_is_removed(self):
        result = strip_narration_comments(
            "# first, let's see what's listening\nnmap -sV -p- 10.0.0.1"
        )
        self.assertEqual(result.command, "nmap -sV -p- 10.0.0.1")
        self.assertTrue(result.was_stripped)
        self.assertEqual(result.stripped, ["first, let's see what's listening"])

    def test_trailing_narration_is_removed_keeping_command(self):
        result = strip_narration_comments("nmap -sV 10.0.0.1  # I'll scan the host")
        self.assertEqual(result.command, "nmap -sV 10.0.0.1")
        self.assertEqual(result.stripped, ["I'll scan the host"])

    def test_multiple_narration_comments(self):
        result = strip_narration_comments(
            "# Step 1: enumerate\n"
            "nmap -p- 10.0.0.1\n"
            "# now let's grab the banner\n"
            "curl -sI http://10.0.0.1"
        )
        self.assertEqual(result.command, "nmap -p- 10.0.0.1\ncurl -sI http://10.0.0.1")
        self.assertEqual(len(result.stripped), 2)

    def test_nudge_mentions_count_and_first_comment(self):
        result = strip_narration_comments("# I'll check the port\nss -tlnp")
        nudge = result.nudge()
        self.assertIsNotNone(nudge)
        self.assertIn("1 narration comment", nudge)
        self.assertIn("I'll check the port", nudge)

    def test_nudge_is_none_when_nothing_stripped(self):
        result = strip_narration_comments("ls -la")
        self.assertIsNone(result.nudge())
        self.assertFalse(result.was_stripped)


class NarrationFalsePositiveTest(unittest.TestCase):
    """`#` that is load-bearing must survive untouched."""

    def _assert_unchanged(self, command):
        result = strip_narration_comments(command)
        self.assertEqual(result.command, command)
        self.assertFalse(result.was_stripped)

    def test_fragment_in_single_quoted_url(self):
        self._assert_unchanged("curl 'http://target/page#section'")

    def test_fragment_in_double_quoted_url(self):
        self._assert_unchanged('curl "http://target/page#frag"')

    def test_hash_inside_sed_expression(self):
        self._assert_unchanged("sed 's/#//g' /etc/hosts")

    def test_hash_mid_word_is_not_a_comment(self):
        self._assert_unchanged("echo item#4")

    def test_escaped_hash(self):
        self._assert_unchanged("echo \\# not a comment")

    def test_operational_comments_are_preserved(self):
        self._assert_unchanged("nmap -sV 10.0.0.1  # nosec")
        self._assert_unchanged("python x.py  # noqa: E501")
        self._assert_unchanged("cmd  # TODO: retry on failure")

    def test_heredoc_body_is_untouched(self):
        command = (
            "cat <<'EOF' > /tmp/note.txt\n"
            "# I'll explain this inside the file\n"
            "payload\n"
            "EOF"
        )
        self._assert_unchanged(command)

    def test_command_that_is_only_narration_is_left_alone(self):
        # Nothing executable would remain; hand it back and let the shell or
        # validation surface the real problem.
        self._assert_unchanged("# I'll figure out the next step")

    def test_awk_program_with_hash_in_quotes(self):
        self._assert_unchanged("awk '{print $1}  # inner' /etc/passwd")


class HeredocTrackingTest(unittest.TestCase):
    def test_narration_after_heredoc_close_is_stripped(self):
        result = strip_narration_comments(
            "cat <<EOF > /tmp/a\ndata\nEOF\n# now let's read it back\ncat /tmp/a"
        )
        self.assertEqual(result.command, "cat <<EOF > /tmp/a\ndata\nEOF\ncat /tmp/a")
        self.assertEqual(result.stripped, ["now let's read it back"])

    def test_narration_before_heredoc_is_stripped(self):
        result = strip_narration_comments(
            "# I'll write the payload file\ncat <<EOF > /tmp/p\n# kept\nEOF"
        )
        self.assertEqual(result.command, "cat <<EOF > /tmp/p\n# kept\nEOF")
        self.assertEqual(result.stripped, ["I'll write the payload file"])


if __name__ == "__main__":
    unittest.main()
