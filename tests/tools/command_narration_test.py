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


class MultilineQuoteStateTest(unittest.TestCase):
    """Quoting spans newlines: a command is one script, not independent lines."""

    def _assert_unchanged(self, command):
        result = strip_narration_comments(command)
        self.assertEqual(result.command, command)
        self.assertFalse(result.was_stripped)

    def test_narration_shaped_line_inside_single_quoted_string(self):
        self._assert_unchanged("ssh host 'echo a\n# Now check the box\necho b'")

    def test_narration_shaped_line_inside_double_quoted_string(self):
        self._assert_unchanged('echo "line one\n# now line two\nline three"')

    def test_python_heredoc_alternative_keeps_its_comments(self):
        # `python3 -c '...'` carries a real Python program; its comments are
        # part of the payload.
        self._assert_unchanged("python3 -c 'print(1)\n# next, print two\nprint(2)'")

    def test_stripping_resumes_after_the_quote_closes(self):
        result = strip_narration_comments(
            "echo 'a\nb'\n# now let's list the directory\nls -la"
        )
        self.assertEqual(result.command, "echo 'a\nb'\nls -la")
        self.assertEqual(result.stripped, ["now let's list the directory"])

    def test_unterminated_quote_stops_stripping(self):
        # The scanner cannot tell what is code any more, so it edits nothing.
        self._assert_unchanged("echo 'unterminated\n# now let's continue\nls")


class HeredocDelimiterFormTest(unittest.TestCase):
    """Every delimiter shape the shell accepts must protect its body."""

    def _assert_body_kept(self, command):
        result = strip_narration_comments(command)
        self.assertEqual(result.command, command)
        self.assertFalse(result.was_stripped)

    def test_bare_delimiter_with_dash(self):
        self._assert_body_kept("cat <<END-MARKER\n# Now this is data\nEND-MARKER")

    def test_single_quoted_delimiter_with_dash(self):
        self._assert_body_kept("cat <<'END-MARKER'\n# Now this is data\nEND-MARKER")

    def test_double_quoted_delimiter(self):
        self._assert_body_kept('cat <<"END.MARKER"\n# Now this is data\nEND.MARKER')

    def test_backslash_escaped_delimiter(self):
        self._assert_body_kept("cat <<\\EOF\n# Now this is data\nEOF")

    def test_delimiter_starting_with_a_digit(self):
        self._assert_body_kept("cat <<'1EOF'\n# first, data\n1EOF")

    def test_delimiter_quoting_is_removed_for_the_terminator_match(self):
        # The shell closes `<<'END-MARKER'` with a bare `END-MARKER` line, so
        # stripping must resume after it.
        result = strip_narration_comments(
            "cat <<'END-MARKER'\n# data\nEND-MARKER\n# now let's read it\ncat x"
        )
        self.assertEqual(
            result.command, "cat <<'END-MARKER'\n# data\nEND-MARKER\ncat x"
        )
        self.assertEqual(result.stripped, ["now let's read it"])

    def test_here_string_opens_no_body(self):
        result = strip_narration_comments(
            "grep x <<< 'haystack'\n# now let's grep again\ngrep y f"
        )
        self.assertEqual(result.command, "grep x <<< 'haystack'\ngrep y f")
        self.assertEqual(result.stripped, ["now let's grep again"])


class HeredocTerminatorTest(unittest.TestCase):
    """The terminator must match the way the shell matches it, exactly."""

    def test_space_indented_terminator_does_not_close_a_plain_heredoc(self):
        command = "cat <<EOF\n  EOF\n# Now this is still data\nEOF"
        result = strip_narration_comments(command)
        self.assertEqual(result.command, command)
        self.assertFalse(result.was_stripped)

    def test_trailing_whitespace_terminator_does_not_close(self):
        command = "cat <<EOF\nEOF \n# Now this is still data\nEOF"
        result = strip_narration_comments(command)
        self.assertEqual(result.command, command)
        self.assertFalse(result.was_stripped)

    def test_tab_indented_terminator_closes_a_dash_heredoc(self):
        result = strip_narration_comments(
            "cat <<-EOF\n\tdata\n\tEOF\n# now let's read it\ncat x"
        )
        self.assertEqual(result.command, "cat <<-EOF\n\tdata\n\tEOF\ncat x")
        self.assertEqual(result.stripped, ["now let's read it"])

    def test_space_indented_terminator_does_not_close_a_dash_heredoc(self):
        # `<<-` strips leading tabs only; spaces are body content.
        command = "cat <<-EOF\n  EOF\n# Now this is still data\n\tEOF"
        result = strip_narration_comments(command)
        self.assertEqual(result.command, command)
        self.assertFalse(result.was_stripped)


class CommandSubstitutionTest(unittest.TestCase):
    def test_comment_inside_bare_substitution_is_a_real_comment(self):
        # bash treats `#` at a word boundary inside `$( )` as a comment, so
        # stripping narration there matches the shell.
        result = strip_narration_comments("out=$(\n# now let's list\nls)\necho $out")
        self.assertEqual(result.command, "out=$(\nls)\necho $out")
        self.assertEqual(result.stripped, ["now let's list"])

    def test_substitution_inside_double_quotes_is_left_alone(self):
        # The scanner stays inside the quotes rather than re-deriving bash's
        # nesting rules. Conservative, and the safe direction to be wrong in.
        command = 'echo "$(\n# now list\nls)"'
        result = strip_narration_comments(command)
        self.assertEqual(result.command, command)

    def test_left_shift_does_not_expose_later_lines(self):
        # `<<` in arithmetic reads as a heredoc opener, which costs a strip but
        # never edits the command.
        command = "x=$((1<<3))\n# now let's echo\necho $x"
        result = strip_narration_comments(command)
        self.assertEqual(result.command, command)
        self.assertFalse(result.was_stripped)


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
