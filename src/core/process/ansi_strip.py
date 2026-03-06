"""
ANSI escape sequence stripper for LLM-facing output.

Removes color codes, cursor movement, progress bars, bracketed paste,
and other terminal control sequences so the LLM receives clean text.
"""

from __future__ import annotations

import re

# Matches CSI sequences: ESC[ ... final_byte
# Covers colors, cursor movement, erase, scroll, etc.
_CSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# Matches OSC sequences: ESC] ... ST (BEL or ESC\)
_OSC = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)")

# Matches single/two-char escape sequences: ESC followed by one char
_ESC_SINGLE = re.compile(r"\x1b[^[\]()]")

# Matches SS2/SS3 sequences
_SS = re.compile(r"\x1b[NO].")

# Carriage return not followed by newline (progress bar overwrites)
_CR_OVERWRITE = re.compile(r"\r(?!\n)")

# Remaining control chars (except \n, \t, \r)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def strip_ansi(text: str) -> str:
    """Strip ANSI escape sequences and terminal control characters from text."""
    result = text
    result = _CSI.sub("", result)
    result = _OSC.sub("", result)
    result = _SS.sub("", result)
    result = _ESC_SINGLE.sub("", result)
    result = _CR_OVERWRITE.sub("", result)
    result = _CONTROL.sub("", result)
    return result
