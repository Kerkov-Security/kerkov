import json
import unittest

from kerkov.core.tool_call_display import (
    MAX_VALUE_CHARS,
    PREVIEW_MAX_LINES,
    summarize_call,
    summarize_output,
)


class SummarizeCallTest(unittest.TestCase):
    def test_short_call_renders_every_argument(self):
        self.assertEqual(
            summarize_call("exec", {"command": "ls -la", "timeout": 30}, 80),
            "exec(command='ls -la', timeout=30)",
        )

    def test_keeps_the_models_argument_order(self):
        summary = summarize_call("http", {"url": "https://x", "method": "GET"}, 80)
        self.assertLess(summary.index("url="), summary.index("method="))

    def test_long_strings_are_cut_and_escaped_onto_one_line(self):
        summary = summarize_call(
            "add_note", {"text": "first line\nsecond ‮" + "x" * 200}, 200
        )
        self.assertNotIn("\n", summary)
        self.assertNotIn("‮", summary)
        self.assertIn("\\n", summary)
        self.assertIn("…", summary)
        self.assertLess(len(summary), MAX_VALUE_CHARS + 40)

    def test_collections_render_as_counts(self):
        summary = summarize_call(
            "add_note",
            {"citations": ["a", "b", "c"], "headers": {"Authorization": "secret"}},
            120,
        )
        self.assertIn("citations=[3 items]", summary)
        self.assertIn("headers={1 key}", summary)
        self.assertNotIn("secret", summary)

    def test_arguments_that_do_not_fit_are_counted(self):
        params = {f"arg{n}": "v" * 30 for n in range(8)}
        summary = summarize_call("mcp__server__tool", params, 100)
        self.assertLessEqual(len(summary), 100)
        self.assertIn("arg0=", summary)
        self.assertRegex(summary, r", \+\d+ more\)$")

    def test_an_oversized_first_argument_is_clipped_to_width(self):
        summary = summarize_call("exec", {"command": "x" * 50}, 30)
        self.assertEqual(len(summary), 30)
        self.assertTrue(summary.endswith("…"))

    def test_write_omits_the_file_body(self):
        summary = summarize_call(
            "write", {"file_path": "a.py", "content": "TOKEN=hunter2\n"}, 80
        )
        self.assertEqual(summary, "write → a.py (py, 1 line)")


class SummarizeOutputTest(unittest.TestCase):
    def test_empty_output_has_no_preview(self):
        self.assertEqual(summarize_output("  \n", 80), [])

    def test_text_is_bounded_and_counts_the_rest(self):
        output = "\n".join(f"line {n}" for n in range(10))
        preview = summarize_output(output, 80)
        self.assertEqual(preview[:PREVIEW_MAX_LINES], ["line 0", "line 1", "line 2"])
        self.assertEqual(preview[-1], "… +7 more lines")

    def test_long_lines_are_clipped_and_tabs_expanded(self):
        preview = summarize_output("a\tb" + "x" * 200, 40)
        self.assertEqual(len(preview[0]), 40)
        self.assertNotIn("\t", preview[0])
        self.assertNotIn("\\t", preview[0])

    def test_json_object_shows_top_level_fields(self):
        output = json.dumps(
            {"id": 6, "status": "active", "citations": ["a", "b"], "text": "y" * 500}
        )
        self.assertEqual(
            summarize_output(output, 200),
            [
                "{id: 6, status: 'active', citations: [2 items], text: '"
                + "y" * MAX_VALUE_CHARS
                + "…'}"
            ],
        )

    def test_json_object_that_does_not_fit_counts_hidden_fields(self):
        output = json.dumps({f"key{n}": "v" * 20 for n in range(10)})
        (line,) = summarize_output(output, 80)
        self.assertLessEqual(len(line), 80)
        self.assertRegex(line, r", \+\d+ more\}$")

    def test_json_array_is_counted(self):
        self.assertEqual(summarize_output("[1, 2, 3]", 80), ["[3 items]"])

    def test_invalid_json_falls_back_to_text(self):
        self.assertEqual(summarize_output("{not json", 80), ["{not json"])


if __name__ == "__main__":
    unittest.main()
