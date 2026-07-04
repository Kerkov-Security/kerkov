"""
ANSI key sequence encoder for PTY interaction.

Converts named key tokens (Enter, Up, Ctrl+C, etc.) into raw ANSI escape
sequences that can be written to a PTY stdin. Also handles bracketed paste mode.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

ESC = "\x1b"
CR = "\r"
TAB = "\t"
BACKSPACE = "\x7f"

BRACKETED_PASTE_START = f"{ESC}[200~"
BRACKETED_PASTE_END = f"{ESC}[201~"

_ESC_PATTERN = re.escape(ESC)
# CSI number sequences: ESC[5~ → ESC[5;mod~
_CSI_NUMBER = re.compile(rf"^{_ESC_PATTERN}\[(\d+)([~A-Z])$")
# CSI arrow sequences: ESC[A → ESC[1;modA
_CSI_ARROW = re.compile(rf"^{_ESC_PATTERN}\[([A-DHF])$")

NAMED_KEY_MAP: dict[str, str] = {
    "enter": CR,
    "return": CR,
    "tab": TAB,
    "escape": ESC,
    "esc": ESC,
    "space": " ",
    "bspace": BACKSPACE,
    "backspace": BACKSPACE,
    "up": f"{ESC}[A",
    "down": f"{ESC}[B",
    "right": f"{ESC}[C",
    "left": f"{ESC}[D",
    "home": f"{ESC}[1~",
    "end": f"{ESC}[4~",
    "pageup": f"{ESC}[5~",
    "pgup": f"{ESC}[5~",
    "ppage": f"{ESC}[5~",
    "pagedown": f"{ESC}[6~",
    "pgdn": f"{ESC}[6~",
    "npage": f"{ESC}[6~",
    "insert": f"{ESC}[2~",
    "ic": f"{ESC}[2~",
    "delete": f"{ESC}[3~",
    "del": f"{ESC}[3~",
    "dc": f"{ESC}[3~",
    "btab": f"{ESC}[Z",
    "f1": f"{ESC}OP",
    "f2": f"{ESC}OQ",
    "f3": f"{ESC}OR",
    "f4": f"{ESC}OS",
    "f5": f"{ESC}[15~",
    "f6": f"{ESC}[17~",
    "f7": f"{ESC}[18~",
    "f8": f"{ESC}[19~",
    "f9": f"{ESC}[20~",
    "f10": f"{ESC}[21~",
    "f11": f"{ESC}[23~",
    "f12": f"{ESC}[24~",
    "kp/": f"{ESC}Oo",
    "kp*": f"{ESC}Oj",
    "kp-": f"{ESC}Om",
    "kp+": f"{ESC}Ok",
    "kp7": f"{ESC}Ow",
    "kp8": f"{ESC}Ox",
    "kp9": f"{ESC}Oy",
    "kp4": f"{ESC}Ot",
    "kp5": f"{ESC}Ou",
    "kp6": f"{ESC}Ov",
    "kp1": f"{ESC}Oq",
    "kp2": f"{ESC}Or",
    "kp3": f"{ESC}Os",
    "kp0": f"{ESC}Op",
    "kp.": f"{ESC}On",
    "kpenter": f"{ESC}OM",
}

# Keys that support xterm modifier encoding
MODIFIABLE_NAMED_KEYS: set[str] = {
    "up",
    "down",
    "left",
    "right",
    "home",
    "end",
    "pageup",
    "pgup",
    "ppage",
    "pagedown",
    "pgdn",
    "npage",
    "insert",
    "ic",
    "delete",
    "del",
    "dc",
}


@dataclass
class Modifiers:
    ctrl: bool = False
    alt: bool = False
    shift: bool = False


@dataclass
class KeyEncodingRequest:
    keys: Optional[list[str]] = None
    hex_bytes: Optional[list[str]] = None
    literal: Optional[str] = None


@dataclass
class KeyEncodingResult:
    data: str = ""
    warnings: list[str] = field(default_factory=list)


def encode_key_sequence(request: KeyEncodingRequest) -> KeyEncodingResult:
    """
    Encode a key request into raw ANSI bytes.

    Processes literal text, hex bytes, and named key tokens in order.
    """
    warnings: list[str] = []
    data = ""

    if request.literal:
        data += request.literal

    if request.hex_bytes:
        for raw in request.hex_bytes:
            byte_val = _parse_hex_byte(raw)
            if byte_val is None:
                warnings.append(f"Invalid hex byte: {raw}")
                continue
            data += chr(byte_val)

    if request.keys:
        for token in request.keys:
            data += _encode_key_token(token, warnings)

    return KeyEncodingResult(data=data, warnings=warnings)


def encode_paste(text: str, bracketed: bool = True) -> str:
    """Wrap text in bracketed paste mode escape sequences."""
    if not bracketed:
        return text
    return f"{BRACKETED_PASTE_START}{text}{BRACKETED_PASTE_END}"


def _encode_key_token(raw: str, warnings: list[str]) -> str:
    """Encode a single key token to its ANSI sequence."""
    token = raw.strip()
    if not token:
        return ""

    # ^X shorthand for Ctrl
    if len(token) == 2 and token[0] == "^":
        ctrl = _to_ctrl_char(token[1])
        if ctrl:
            return ctrl

    parsed_mods, base, has_modifiers = _parse_modifiers(token)
    base_lower = base.lower()

    # Shift+Tab special case
    if base_lower == "tab" and parsed_mods.shift:
        return f"{ESC}[Z"

    base_seq = NAMED_KEY_MAP.get(base_lower)
    if base_seq is not None:
        seq = base_seq
        if base_lower in MODIFIABLE_NAMED_KEYS and _has_any_modifier(parsed_mods):
            mod = _xterm_modifier(parsed_mods)
            if mod > 1:
                modified = _apply_xterm_modifier(seq, mod)
                if modified:
                    return modified
        if parsed_mods.alt:
            return f"{ESC}{seq}"
        return seq

    # Single character with modifiers
    if len(base) == 1:
        return _apply_char_modifiers(base, parsed_mods)

    if has_modifiers:
        warnings.append(f'Unknown key "{base}" for modifiers; sending literal.')
    return base


def _parse_modifiers(token: str) -> tuple[Modifiers, str, bool]:
    """
    Parse modifier prefixes from a key token.

    Supports C- (ctrl), M- (alt), S- (shift) prefixes.
    Returns (modifiers, remaining_base, had_modifiers).
    """
    mods = Modifiers()
    rest = token
    saw_modifiers = False

    while len(rest) > 2 and rest[1] == "-":
        mod = rest[0].lower()
        if mod == "c":
            mods.ctrl = True
        elif mod == "m":
            mods.alt = True
        elif mod == "s":
            mods.shift = True
        else:
            break
        saw_modifiers = True
        rest = rest[2:]

    return mods, rest, saw_modifiers


def _apply_char_modifiers(char: str, mods: Modifiers) -> str:
    """Apply modifier keys to a single character."""
    value = char
    if mods.shift and len(value) == 1 and value.isalpha() and value.islower():
        value = value.upper()
    if mods.ctrl:
        ctrl = _to_ctrl_char(value)
        if ctrl:
            value = ctrl
    if mods.alt:
        value = f"{ESC}{value}"
    return value


def _to_ctrl_char(char: str) -> Optional[str]:
    """Convert a character to its Ctrl+key equivalent."""
    if len(char) != 1:
        return None
    if char == "?":
        return "\x7f"
    code = ord(char.upper())
    if 64 <= code <= 95:
        return chr(code & 0x1F)
    return None


def _xterm_modifier(mods: Modifiers) -> int:
    """Calculate xterm modifier number (1-based)."""
    mod = 1
    if mods.shift:
        mod += 1
    if mods.alt:
        mod += 2
    if mods.ctrl:
        mod += 4
    return mod


def _apply_xterm_modifier(sequence: str, modifier: int) -> Optional[str]:
    """
    Apply xterm modifier encoding to a CSI sequence.

    Transforms e.g. ESC[5~ → ESC[5;2~ for Shift modifier.
    """
    m = _CSI_NUMBER.match(sequence)
    if m:
        return f"{ESC}[{m.group(1)};{modifier}{m.group(2)}"

    m = _CSI_ARROW.match(sequence)
    if m:
        return f"{ESC}[1;{modifier}{m.group(1)}"

    return None


def _has_any_modifier(mods: Modifiers) -> bool:
    return mods.ctrl or mods.alt or mods.shift


def _parse_hex_byte(raw: str) -> Optional[int]:
    """Parse a hex byte string like '0x04' or '1b' into an integer 0-255."""
    trimmed = raw.strip().lower()
    normalized = trimmed[2:] if trimmed.startswith("0x") else trimmed
    if not re.match(r"^[0-9a-f]{1,2}$", normalized):
        return None
    value = int(normalized, 16)
    if value < 0 or value > 0xFF:
        return None
    return value
