import unittest
from src.core.process.key_encoder import (
    KeyEncodingRequest, 
    encode_key_sequence, 
    _to_ctrl_char, 
    _parse_modifiers,
    encode_paste,
)

class KeyEncoderTest(unittest.TestCase):

    def test_ctrl_char_conversion(self):
        """Test the bitwise Ctrl logic we discussed."""
        self.assertEqual(_to_ctrl_char("a"), "\x01")  # Ctrl+A
        self.assertEqual(_to_ctrl_char("z"), "\x1a")  # Ctrl+Z
        self.assertEqual(_to_ctrl_char("?"), "\x7f")  # Special case: Delete
        self.assertIsNone(_to_ctrl_char("1"))         # Invalid range

    def test_modifier_parsing(self):
        """Ensure 'C-M-s-x' style strings parse correctly."""
        mods, base, had_mods = _parse_modifiers("C-M-S-a")
        self.assertTrue(mods.ctrl)
        self.assertTrue(mods.alt)
        self.assertTrue(mods.shift)
        self.assertEqual(base, "a")
        self.assertTrue(had_mods)

    def test_basic_named_keys(self):
        """Test standard keys like Enter and Tab."""
        req = KeyEncodingRequest(keys=["enter", "tab", "esc"])
        result = encode_key_sequence(req)
        # \r = Enter, \t = Tab, \x1b = Escape
        self.assertEqual(result.data, "\r\t\x1b")

    def test_xterm_modifiers(self):
        """Test the '1;modifier' encoding for arrow keys."""
        # Shift + Up should be ESC[1;2A
        req = KeyEncodingRequest(keys=["S-up"])
        result = encode_key_sequence(req)
        self.assertEqual(result.data, "\x1b[1;2A")
        
        # Ctrl + Down should be ESC[1;5B
        req = KeyEncodingRequest(keys=["C-down"])
        result = encode_key_sequence(req)
        self.assertEqual(result.data, "\x1b[1;5B")

    def test_hex_and_literal(self):
        """Test mixing raw text and hex bytes."""
        req = KeyEncodingRequest(
            literal="hi", 
            hex_bytes=["0x0d"] # CR (Enter)
        )
        result = encode_key_sequence(req)
        self.assertEqual(result.data, "hi\r")

    def test_bracketed_paste(self):
        """Test wrapping text for paste mode."""
        pasted = encode_paste("hello", bracketed=True)
        # Should start with ESC[200~ and end with ESC[201~
        self.assertTrue(pasted.startswith("\x1b[200~"))
        self.assertTrue(pasted.endswith("\x1b[201~"))
        self.assertIn("hello", pasted)

    def test_invalid_hex_warning(self):
        """Ensure bad hex input triggers a warning instead of crashing."""
        req = KeyEncodingRequest(hex_bytes=["ZZ"]) # Not hex
        result = encode_key_sequence(req)
        self.assertIn("Invalid hex byte: ZZ", result.warnings)

if __name__ == "__main__":
    unittest.main()
    