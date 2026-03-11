import unittest

from src.core.process.key_encoder import (
    ESC,
    KeyEncodingRequest,
    KeyEncodingResult,
    Modifiers,
    encode_key_sequence,
    encode_paste,
    _apply_char_modifiers,
    _apply_xterm_modifier,
    _encode_key_token,
    _has_any_modifier,
    _parse_hex_byte,
    _parse_modifiers,
    _to_ctrl_char,
    _xterm_modifier,
)


# ---------------------------------------------------------------------------
# _to_ctrl_char
# ---------------------------------------------------------------------------

class ToCtrlCharTest(unittest.TestCase):

    def test_ctrl_a(self):
        assert _to_ctrl_char("a") == "\x01"

    def test_ctrl_z(self):
        assert _to_ctrl_char("z") == "\x1a"

    def test_ctrl_c_uppercase(self):
        assert _to_ctrl_char("C") == "\x03"

    def test_ctrl_at_sign(self):
        assert _to_ctrl_char("@") == "\x00"

    def test_ctrl_underscore(self):
        assert _to_ctrl_char("_") == "\x1f"

    def test_ctrl_question_mark_is_del(self):
        assert _to_ctrl_char("?") == "\x7f"

    def test_returns_none_for_digit(self):
        assert _to_ctrl_char("1") is None

    def test_returns_none_for_multi_char(self):
        assert _to_ctrl_char("ab") is None

    def test_returns_none_for_empty(self):
        assert _to_ctrl_char("") is None


# ---------------------------------------------------------------------------
# _parse_modifiers
# ---------------------------------------------------------------------------

class ParseModifiersTest(unittest.TestCase):

    def test_no_modifiers(self):
        mods, base, had = _parse_modifiers("a")
        assert base == "a"
        assert not had
        assert not mods.ctrl and not mods.alt and not mods.shift

    def test_ctrl_only(self):
        mods, base, had = _parse_modifiers("C-a")
        assert mods.ctrl and not mods.alt and not mods.shift
        assert base == "a"
        assert had

    def test_alt_only(self):
        mods, base, had = _parse_modifiers("M-x")
        assert mods.alt and not mods.ctrl
        assert base == "x"

    def test_shift_only(self):
        mods, base, had = _parse_modifiers("S-tab")
        assert mods.shift
        assert base == "tab"

    def test_all_modifiers_combined(self):
        mods, base, had = _parse_modifiers("C-M-S-a")
        assert mods.ctrl and mods.alt and mods.shift
        assert base == "a"
        assert had

    def test_unknown_modifier_prefix_stops_parsing(self):
        mods, base, had = _parse_modifiers("X-a")
        assert not had
        assert base == "X-a"

    def test_case_insensitive_modifier(self):
        mods, base, _ = _parse_modifiers("c-a")
        assert mods.ctrl
        assert base == "a"

    def test_named_key_base(self):
        mods, base, had = _parse_modifiers("C-up")
        assert mods.ctrl
        assert base == "up"


# ---------------------------------------------------------------------------
# _xterm_modifier
# ---------------------------------------------------------------------------

class XtermModifierTest(unittest.TestCase):

    def test_no_modifiers_returns_1(self):
        assert _xterm_modifier(Modifiers()) == 1

    def test_shift_returns_2(self):
        assert _xterm_modifier(Modifiers(shift=True)) == 2

    def test_alt_returns_3(self):
        assert _xterm_modifier(Modifiers(alt=True)) == 3

    def test_ctrl_returns_5(self):
        assert _xterm_modifier(Modifiers(ctrl=True)) == 5

    def test_shift_alt_returns_4(self):
        assert _xterm_modifier(Modifiers(shift=True, alt=True)) == 4

    def test_ctrl_shift_returns_6(self):
        assert _xterm_modifier(Modifiers(ctrl=True, shift=True)) == 6

    def test_ctrl_alt_returns_7(self):
        assert _xterm_modifier(Modifiers(ctrl=True, alt=True)) == 7

    def test_all_modifiers_returns_8(self):
        assert _xterm_modifier(Modifiers(ctrl=True, alt=True, shift=True)) == 8


# ---------------------------------------------------------------------------
# _apply_xterm_modifier
# ---------------------------------------------------------------------------

class ApplyXtermModifierTest(unittest.TestCase):

    def test_csi_number_sequence(self):
        assert _apply_xterm_modifier(f"{ESC}[5~", 2) == f"{ESC}[5;2~"

    def test_csi_arrow_sequence(self):
        assert _apply_xterm_modifier(f"{ESC}[A", 5) == f"{ESC}[1;5A"

    def test_returns_none_for_unrecognized_sequence(self):
        assert _apply_xterm_modifier("not-a-sequence", 2) is None

    def test_does_not_match_pipe_character(self):
        assert _apply_xterm_modifier(f"{ESC}[|", 2) is None


# ---------------------------------------------------------------------------
# _apply_char_modifiers
# ---------------------------------------------------------------------------

class ApplyCharModifiersTest(unittest.TestCase):

    def test_no_modifiers_passes_through(self):
        assert _apply_char_modifiers("a", Modifiers()) == "a"

    def test_shift_uppercases_letter(self):
        assert _apply_char_modifiers("a", Modifiers(shift=True)) == "A"

    def test_shift_ignores_already_upper(self):
        assert _apply_char_modifiers("A", Modifiers(shift=True)) == "A"

    def test_shift_ignores_non_alpha(self):
        assert _apply_char_modifiers("1", Modifiers(shift=True)) == "1"

    def test_ctrl_converts_to_ctrl_char(self):
        assert _apply_char_modifiers("c", Modifiers(ctrl=True)) == "\x03"

    def test_alt_prepends_esc(self):
        assert _apply_char_modifiers("x", Modifiers(alt=True)) == f"{ESC}x"

    def test_ctrl_alt_combined(self):
        result = _apply_char_modifiers("c", Modifiers(ctrl=True, alt=True))
        assert result == f"{ESC}\x03"

    def test_shift_ctrl_uppercases_then_ctrl(self):
        result = _apply_char_modifiers("a", Modifiers(shift=True, ctrl=True))
        assert result == "\x01"


# ---------------------------------------------------------------------------
# _has_any_modifier
# ---------------------------------------------------------------------------

class HasAnyModifierTest(unittest.TestCase):

    def test_none_set(self):
        assert not _has_any_modifier(Modifiers())

    def test_ctrl_set(self):
        assert _has_any_modifier(Modifiers(ctrl=True))

    def test_alt_set(self):
        assert _has_any_modifier(Modifiers(alt=True))

    def test_shift_set(self):
        assert _has_any_modifier(Modifiers(shift=True))


# ---------------------------------------------------------------------------
# _parse_hex_byte
# ---------------------------------------------------------------------------

class ParseHexByteTest(unittest.TestCase):

    def test_with_0x_prefix(self):
        assert _parse_hex_byte("0x0d") == 0x0D

    def test_without_prefix(self):
        assert _parse_hex_byte("1b") == 0x1B

    def test_single_digit(self):
        assert _parse_hex_byte("a") == 0x0A

    def test_max_value(self):
        assert _parse_hex_byte("ff") == 0xFF

    def test_zero(self):
        assert _parse_hex_byte("00") == 0

    def test_strips_whitespace(self):
        assert _parse_hex_byte("  0x0a  ") == 0x0A

    def test_invalid_hex_returns_none(self):
        assert _parse_hex_byte("ZZ") is None

    def test_too_many_digits_returns_none(self):
        assert _parse_hex_byte("0x123") is None

    def test_empty_string_returns_none(self):
        assert _parse_hex_byte("") is None


# ---------------------------------------------------------------------------
# _encode_key_token
# ---------------------------------------------------------------------------

class EncodeKeyTokenTest(unittest.TestCase):

    def test_empty_token(self):
        assert _encode_key_token("", []) == ""

    def test_whitespace_only_token(self):
        assert _encode_key_token("   ", []) == ""

    def test_caret_shorthand_ctrl_c(self):
        assert _encode_key_token("^C", []) == "\x03"

    def test_caret_shorthand_ctrl_d(self):
        assert _encode_key_token("^D", []) == "\x04"

    def test_shift_tab_special_case(self):
        assert _encode_key_token("S-tab", []) == f"{ESC}[Z"

    def test_named_key_enter(self):
        assert _encode_key_token("enter", []) == "\r"

    def test_named_key_case_insensitive(self):
        assert _encode_key_token("Enter", []) == "\r"

    def test_arrow_key_up(self):
        assert _encode_key_token("up", []) == f"{ESC}[A"

    def test_function_key_f1(self):
        assert _encode_key_token("f1", []) == f"{ESC}OP"

    def test_function_key_f12(self):
        assert _encode_key_token("f12", []) == f"{ESC}[24~"

    def test_modifier_on_arrow_key(self):
        assert _encode_key_token("S-up", []) == f"{ESC}[1;2A"

    def test_ctrl_on_modifiable_key(self):
        assert _encode_key_token("C-delete", []) == f"{ESC}[3;5~"

    def test_alt_on_non_modifiable_named_key(self):
        assert _encode_key_token("M-enter", []) == f"{ESC}\r"

    def test_single_char_without_modifiers(self):
        assert _encode_key_token("a", []) == "a"

    def test_unknown_key_with_modifiers_emits_warning(self):
        warnings = []
        result = _encode_key_token("C-foobar", warnings)
        assert result == "foobar"
        assert len(warnings) == 1
        assert "foobar" in warnings[0]

    def test_unknown_multichar_without_modifiers_returns_literal(self):
        warnings = []
        result = _encode_key_token("foobar", warnings)
        assert result == "foobar"
        assert len(warnings) == 0

    def test_strips_surrounding_whitespace(self):
        assert _encode_key_token("  enter  ", []) == "\r"


# ---------------------------------------------------------------------------
# encode_key_sequence (integration)
# ---------------------------------------------------------------------------

class EncodeKeySequenceTest(unittest.TestCase):

    def test_basic_named_keys(self):
        req = KeyEncodingRequest(keys=["enter", "tab", "esc"])
        result = encode_key_sequence(req)
        assert result.data == "\r\t\x1b"
        assert result.warnings == []

    def test_literal_only(self):
        req = KeyEncodingRequest(literal="hello")
        result = encode_key_sequence(req)
        assert result.data == "hello"

    def test_hex_bytes_only(self):
        req = KeyEncodingRequest(hex_bytes=["0x0d", "0x0a"])
        result = encode_key_sequence(req)
        assert result.data == "\r\n"

    def test_literal_then_hex_then_keys_ordering(self):
        req = KeyEncodingRequest(literal="a", hex_bytes=["0x0d"], keys=["tab"])
        result = encode_key_sequence(req)
        assert result.data == "a\r\t"

    def test_invalid_hex_produces_warning(self):
        req = KeyEncodingRequest(hex_bytes=["ZZ"])
        result = encode_key_sequence(req)
        assert "Invalid hex byte: ZZ" in result.warnings
        assert result.data == ""

    def test_mixed_valid_and_invalid_hex(self):
        req = KeyEncodingRequest(hex_bytes=["0x0d", "ZZ", "0x0a"])
        result = encode_key_sequence(req)
        assert result.data == "\r\n"
        assert len(result.warnings) == 1

    def test_empty_request(self):
        req = KeyEncodingRequest()
        result = encode_key_sequence(req)
        assert result.data == ""
        assert result.warnings == []

    def test_multiple_keys(self):
        req = KeyEncodingRequest(keys=["^C", "enter"])
        result = encode_key_sequence(req)
        assert result.data == "\x03\r"


# ---------------------------------------------------------------------------
# encode_paste
# ---------------------------------------------------------------------------

class EncodePasteTest(unittest.TestCase):

    def test_wraps_with_bracketed_paste(self):
        result = encode_paste("hello")
        assert result == f"{ESC}[200~hello{ESC}[201~"

    def test_bracketed_false_returns_text_unchanged(self):
        assert encode_paste("hello", bracketed=False) == "hello"

    def test_empty_string(self):
        result = encode_paste("")
        assert result == f"{ESC}[200~{ESC}[201~"

    def test_multiline_text(self):
        text = "line1\nline2"
        result = encode_paste(text)
        assert text in result
        assert result.startswith(f"{ESC}[200~")
        assert result.endswith(f"{ESC}[201~")


if __name__ == "__main__":
    unittest.main()
