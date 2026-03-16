import unittest

from src.core.process.ansi_strip import strip_ansi


class StripAnsiTest(unittest.TestCase):

    # --- Passthrough (no escapes) ---

    def test_plain_text_unchanged(self):
        assert strip_ansi("hello world") == "hello world"

    def test_empty_string(self):
        assert strip_ansi("") == ""

    def test_preserves_newlines_and_tabs(self):
        assert strip_ansi("line1\n\tline2\n") == "line1\n\tline2\n"

    # --- CSI sequences (ESC[ ... letter) ---

    def test_strips_sgr_color_codes(self):
        assert strip_ansi("\x1b[31mred\x1b[0m") == "red"

    def test_strips_bold_and_reset(self):
        assert strip_ansi("\x1b[1mbold\x1b[22m") == "bold"

    def test_strips_multiple_sgr_params(self):
        assert strip_ansi("\x1b[1;31;42mtext\x1b[0m") == "text"

    def test_strips_256_color(self):
        assert strip_ansi("\x1b[38;5;196mred\x1b[0m") == "red"

    def test_strips_24bit_color(self):
        assert strip_ansi("\x1b[38;2;255;0;0mred\x1b[0m") == "red"

    def test_strips_cursor_movement(self):
        assert strip_ansi("\x1b[5Aup\x1b[3Bdown") == "updown"

    def test_strips_erase_line(self):
        assert strip_ansi("\x1b[2Kcleared") == "cleared"

    def test_strips_erase_display(self):
        assert strip_ansi("\x1b[2Jscreen") == "screen"

    def test_strips_csi_with_question_mark(self):
        assert strip_ansi("\x1b[?25lhidden cursor\x1b[?25h") == "hidden cursor"

    # --- OSC sequences (ESC] ... BEL/ST) ---

    def test_strips_osc_terminated_by_bel(self):
        assert strip_ansi("\x1b]0;window title\x07text") == "text"

    def test_strips_osc_terminated_by_st(self):
        assert strip_ansi("\x1b]0;window title\x1b\\text") == "text"

    # --- SS2/SS3 sequences ---

    def test_strips_ss3_sequence(self):
        assert strip_ansi("\x1bOPtext") == "text"

    def test_strips_ss2_sequence(self):
        assert strip_ansi("\x1bNAtext") == "text"

    # --- Single-char escape sequences ---

    def test_strips_esc_equals(self):
        assert strip_ansi("\x1b=text") == "text"

    def test_strips_esc_greater(self):
        assert strip_ansi("\x1b>text") == "text"

    # --- Carriage return overwrites ---

    def test_strips_cr_without_newline(self):
        assert strip_ansi("progress\roverwrite") == "progressoverwrite"

    def test_preserves_cr_followed_by_newline(self):
        assert strip_ansi("line1\r\nline2") == "line1\r\nline2"

    # --- Control characters ---

    def test_strips_null_byte(self):
        assert strip_ansi("hel\x00lo") == "hello"

    def test_strips_backspace(self):
        assert strip_ansi("hel\x08lo") == "hello"

    def test_strips_bell(self):
        assert strip_ansi("text\x07more") == "textmore"

    def test_strips_delete(self):
        assert strip_ansi("text\x7fmore") == "textmore"

    def test_preserves_carriage_return_before_newline(self):
        assert strip_ansi("a\r\nb") == "a\r\nb"

    # --- Mixed / realistic ---

    def test_strips_colored_ls_output(self):
        line = "\x1b[0m\x1b[01;34mdir\x1b[0m  \x1b[00mfile.txt\x1b[0m"
        assert strip_ansi(line) == "dir  file.txt"

    def test_strips_progress_bar_overwrite(self):
        line = "Downloading... 50%\rDownloading... 100%"
        assert strip_ansi(line) == "Downloading... 50%Downloading... 100%"

    def test_strips_mixed_csi_and_osc(self):
        text = "\x1b]0;title\x07\x1b[32mgreen\x1b[0m"
        assert strip_ansi(text) == "green"

    def test_strips_nested_formatting(self):
        text = "\x1b[1m\x1b[31m\x1b[4munderlined bold red\x1b[0m"
        assert strip_ansi(text) == "underlined bold red"

    def test_multiple_lines_with_escapes(self):
        text = "\x1b[31mline1\x1b[0m\n\x1b[32mline2\x1b[0m\n"
        assert strip_ansi(text) == "line1\nline2\n"


if __name__ == "__main__":
    unittest.main()
