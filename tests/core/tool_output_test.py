"""Tests for the reference formats that stand in for elided tool output."""

import unittest

from kerkov.core.tool_output import (
    EVICTED_HEAD_CHARS,
    EVICTED_TAIL_CHARS,
    PREVIEW_HEAD_CHARS,
    PREVIEW_TAIL_CHARS,
    TOOL_OUTPUT_MARKER,
    build_clipped_output,
    build_evicted_tool_clip,
    split_tool_output,
)


class BuildEvictedToolClipTest(unittest.TestCase):
    def test_keeps_both_ends_of_a_long_output(self):
        clip = build_evicted_tool_clip("HEAD" + "x" * 20_000 + "TAIL")

        self.assertIn("HEAD", clip)
        self.assertIn("TAIL", clip)
        self.assertIn("characters elided", clip)

    def test_short_multibyte_output_is_not_duplicated(self):
        # The eviction gate counts bytes while the slices count characters, so
        # a body can be over the 1 KiB floor and still be shorter than head +
        # tail. Taking a tail there would repeat what the head already holds.
        output = "€" * (EVICTED_HEAD_CHARS + EVICTED_TAIL_CHARS - 100)

        clip = build_evicted_tool_clip(output)

        self.assertNotIn("elided", clip)
        self.assertEqual(clip.count("€"), EVICTED_HEAD_CHARS)

    def test_never_reports_a_negative_elision(self):
        clip = build_evicted_tool_clip("y" * (EVICTED_HEAD_CHARS + 1))

        self.assertNotIn("-", clip.split("Preview follows.]")[0])
        self.assertNotIn("elided", clip)


class BuildClippedOutputTest(unittest.TestCase):
    def test_short_multibyte_output_is_not_duplicated(self):
        output = "€" * (PREVIEW_HEAD_CHARS + PREVIEW_TAIL_CHARS - 100)

        clipped = build_clipped_output(output, artifact_id=3)

        self.assertNotIn("elided", clipped)
        self.assertEqual(clipped.count("€"), PREVIEW_HEAD_CHARS)

    def test_long_output_keeps_a_tail(self):
        output = "H" + "x" * (PREVIEW_HEAD_CHARS + PREVIEW_TAIL_CHARS) + "T"

        clipped = build_clipped_output(output, artifact_id=3)

        self.assertTrue(clipped.rstrip().endswith("T"))
        self.assertIn("artifact#3", clipped)


class SplitToolOutputTest(unittest.TestCase):
    def test_splits_at_the_marker(self):
        header, body = split_tool_output(
            f"Tool 'x' execution result:\nStatus: Failed\n{TOOL_OUTPUT_MARKER}the body"
        )

        self.assertIn("Status: Failed", header)
        self.assertEqual(body, "the body")

    def test_body_containing_the_marker_stays_in_the_body(self):
        header, body = split_tool_output(
            f"Status: Success\n{TOOL_OUTPUT_MARKER}first\n{TOOL_OUTPUT_MARKER}second"
        )

        self.assertEqual(header, f"Status: Success\n{TOOL_OUTPUT_MARKER}")
        self.assertEqual(body, f"first\n{TOOL_OUTPUT_MARKER}second")

    def test_unmarked_content_is_all_body(self):
        header, body = split_tool_output("Tool execution interrupted by user.")

        self.assertEqual(header, "")
        self.assertEqual(body, "Tool execution interrupted by user.")


if __name__ == "__main__":
    unittest.main()
