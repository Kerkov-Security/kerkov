"""Tests for the reference formats that stand in for elided tool output."""

import unittest

from kerkov.core.tool_output import (
    EVICTED_HEAD_CHARS,
    EVICTED_TAIL_CHARS,
    TOOL_OUTPUT_MARKER,
    build_clipped_metadata,
    build_clipped_output,
    build_evicted_tool_clip,
    normalize_tool_output,
    split_tool_output,
)


class NormalizeToolOutputTest(unittest.TestCase):
    def test_default_preview_retains_the_existing_2000_character_budget(self):
        output = "H" * 1500 + "M" * 21_689 + "T" * 500

        preview, meta = normalize_tool_output(output)

        self.assertIn("H" * 1500, preview)
        self.assertTrue(preview.endswith("T" * 500))
        self.assertNotIn("M", preview)
        self.assertEqual(meta.elided_size, len(output) - 2000)
        # Allow the size annotation and elision marker, but guard against
        # enlarging the old KB preview to several thousand extra characters.
        self.assertLess(len(preview), 2200)

    def test_small_output_is_unchanged_including_repetitions_and_line_endings(self):
        for output in ("", "a\r\na\r\na\r\n", "one\ntwo", "€" * 10):
            with self.subTest(output=output):
                preview, meta = normalize_tool_output(output, max_chars=10)

                self.assertEqual(preview, output)
                self.assertEqual(meta.original_size, len(output))
                self.assertFalse(meta.was_clipped)
                self.assertEqual(meta.lines_omitted, 0)
                self.assertEqual(meta.elided_size, 0)

    def test_long_output_keeps_both_ends_and_reports_character_counts(self):
        output = "HEAD" + "€" * 100 + "TAIL"

        preview, meta = normalize_tool_output(output, max_chars=50, head=10, tail=8)

        self.assertIn("clipped: 108 original characters", preview.splitlines()[0])
        self.assertIn(output[:10], preview)
        self.assertTrue(preview.endswith(output[-8:]))
        self.assertIn("middle 90 characters elided", preview)
        self.assertEqual(meta.original_size, 108)
        self.assertEqual(meta.elided_size, 90)
        self.assertTrue(meta.was_clipped)

    def test_collapses_repeated_lines_before_deciding_whether_to_elide(self):
        line = "the same long progress message " * 2
        output = (line + "\n") * 20

        preview, meta = normalize_tool_output(output, max_chars=150)

        self.assertEqual(preview.count(line), 1)
        self.assertIn("… (19 similar lines omitted) …", preview)
        self.assertIn("19 repeated lines omitted", preview.splitlines()[0])
        self.assertNotIn("characters elided", preview)
        self.assertEqual(meta.lines_omitted, 19)
        self.assertEqual(meta.elided_size, 0)
        self.assertTrue(meta.was_clipped)
        self.assertTrue(preview.endswith("\n"))

    def test_whitespace_variants_preserve_first_line_and_final_newline_state(self):
        line = "  meaningful indentation and a long repeated progress message"
        output = line + " \t\r\n" + line + "\t\r\n" + line

        preview, meta = normalize_tool_output(output, max_chars=120)

        self.assertIn(line + " \t\r\n… (2 similar lines omitted) …", preview)
        self.assertEqual(meta.lines_omitted, 2)
        self.assertFalse(preview.endswith("\n"))

    def test_crlf_run_retains_crlf_terminator(self):
        line = "long repeated progress message " * 2

        preview, meta = normalize_tool_output((line + "\r\n") * 5, max_chars=120)

        self.assertIn(line + "\r\n… (4 similar lines omitted) …\r\n", preview)
        self.assertEqual(meta.lines_omitted, 4)

    def test_preserves_distinct_findings_and_nonconsecutive_repetitions(self):
        examples = (
            "\n".join(f"http://target/item/{i} status 200" for i in range(20)),
            "\n".join(" " * i + "same text" for i in range(20)),
            "first repeated line\nsecond repeated line\n" * 20,
        )
        for output in examples:
            with self.subTest(output=output):
                _, meta = normalize_tool_output(output, max_chars=100)

                self.assertEqual(meta.lines_omitted, 0)

    def test_requires_three_copies_and_a_marker_that_saves_space(self):
        for output in (("a long repeated line " * 10 + "\n") * 2, "\n" * 4):
            with self.subTest(output=output):
                _, meta = normalize_tool_output(output, max_chars=1, head=1, tail=0)

                self.assertEqual(meta.lines_omitted, 0)

    def test_multiple_runs_include_a_run_at_end_of_output(self):
        first = "first long repeated progress message"
        last = "last long repeated progress message"
        output = (first + "\n") * 10 + "separator\n" + (last + "\n") * 10

        preview, meta = normalize_tool_output(output, max_chars=180)

        self.assertIn("separator\n", preview)
        self.assertEqual(preview.count("(9 similar lines omitted)"), 2)
        self.assertEqual(meta.lines_omitted, 18)

    def test_elision_counts_only_text_remaining_after_collapse(self):
        line = "same repeated payload " * 5
        output = "H" * 100 + "\n" + (line + "\n") * 10 + "T" * 100
        collapsed = (
            "H" * 100 + "\n" + line + "\n… (9 similar lines omitted) …\n" + "T" * 100
        )

        preview, meta = normalize_tool_output(output, max_chars=100, head=30, tail=20)

        self.assertIn("characters elided after repeated-line collapse", preview)
        self.assertEqual(meta.elided_size, len(collapsed) - 50)
        self.assertEqual(meta.original_size, len(output))
        self.assertEqual(meta.lines_omitted, 9)

    def test_oversized_head_and_tail_scale_to_content_budget(self):
        output = "0123456789" * 10

        preview, meta = normalize_tool_output(output, max_chars=10, head=12, tail=8)

        self.assertIn("\n012345\n", preview)
        self.assertTrue(preview.endswith("6789"))
        self.assertEqual(meta.elided_size, 90)

    def test_zero_head_or_tail_does_not_duplicate_output(self):
        for head, tail in ((0, 8), (8, 0)):
            with self.subTest(head=head, tail=tail):
                preview, meta = normalize_tool_output(
                    "€" * 100, max_chars=10, head=head, tail=tail
                )

                self.assertEqual(preview.count("€"), 8)
                self.assertEqual(meta.elided_size, 92)

    def test_rejects_invalid_budgets(self):
        for kwargs in (
            {"max_chars": 0},
            {"max_chars": -1},
            {"head": -1},
            {"tail": -1},
            {"head": 0, "tail": 0},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                normalize_tool_output("", **kwargs)


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
    def test_supplied_normalized_preview_is_not_clipped_again(self):
        raw = "x" * 10_000
        preview = "normalized preview " * 200

        clipped = build_clipped_output(raw, artifact_id=3, normalized_preview=preview)

        self.assertIn("artifact#3", clipped)
        self.assertIn("9.8 KB", clipped)
        self.assertTrue(clipped.endswith(preview))
        self.assertNotIn("characters elided", clipped)

    def test_short_multibyte_output_is_not_duplicated(self):
        output = "€" * 1900
        preview, _ = normalize_tool_output(output)

        clipped = build_clipped_output(
            output, artifact_id=3, normalized_preview=preview
        )

        self.assertNotIn("elided", clipped)
        self.assertEqual(clipped.count("€"), len(output))

    def test_reference_names_the_retrieval_call_and_original_size(self):
        for size, unit in ((50_000, "KB"), (2_000_000, "MB")):
            with self.subTest(size=size):
                clipped = build_clipped_output(
                    "x" * size, artifact_id=42, normalized_preview="preview"
                )

                self.assertIn("recall(query='artifact#42')", clipped)
                self.assertIn(unit, clipped)


class BuildClippedMetadataTest(unittest.TestCase):
    def test_inline_marker_does_not_claim_output_was_stored(self):
        metadata = {"status": 200, "parsed": "x" * 1000}

        clipped = build_clipped_metadata(metadata, artifact_id=None)

        self.assertEqual(clipped["status"], 200)
        self.assertIn("of 'parsed' kept out of this conversation", clipped["parsed"])
        self.assertNotIn("artifact#", clipped["parsed"])
        self.assertNotIn("stored", clipped["parsed"])
        self.assertEqual(metadata["parsed"], "x" * 1000)


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
