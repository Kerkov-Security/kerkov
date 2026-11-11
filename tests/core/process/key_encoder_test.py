import pytest

from kerkov.core.process.key_encoder import (
    ESC,
    KeyEncodingRequest,
    encode_key_sequence,
    encode_paste,
)


def _keys(*tokens: str) -> str:
    result = encode_key_sequence(KeyEncodingRequest(keys=list(tokens)))
    assert result.warnings == []
    return result.data


@pytest.mark.parametrize(
    "token,expected",
    [
        ("", ""),
        ("  enter  ", "\r"),
        ("Enter", "\r"),
        ("f1", f"{ESC}OP"),
        ("f12", f"{ESC}[24~"),
        ("^C", "\x03"),
        ("^?", "\x7f"),
        ("^1", "^1"),
        ("a", "a"),
        ("S-a", "A"),
        ("S-1", "1"),
        ("C-c", "\x03"),
        ("c-a", "\x01"),
        ("C-@", "\x00"),
        ("C-S-a", "\x01"),
        ("M-x", f"{ESC}x"),
        ("M-C-c", f"{ESC}\x03"),
        ("S-tab", f"{ESC}[Z"),
        ("M-enter", f"{ESC}\r"),
        ("S-enter", "\r"),
        ("X-a", "X-a"),
        ("foobar", "foobar"),
    ],
)
def test_key_token(token: str, expected: str) -> None:
    assert _keys(token) == expected


@pytest.mark.parametrize(
    "token,expected",
    [
        ("up", f"{ESC}[A"),
        ("S-up", f"{ESC}[1;2A"),
        ("M-up", f"{ESC}[1;3A"),
        ("M-S-up", f"{ESC}[1;4A"),
        ("C-up", f"{ESC}[1;5A"),
        ("C-S-up", f"{ESC}[1;6A"),
        ("C-M-up", f"{ESC}[1;7A"),
        ("C-M-S-up", f"{ESC}[1;8A"),
        ("C-delete", f"{ESC}[3;5~"),
        ("S-pagedown", f"{ESC}[6;2~"),
    ],
)
def test_xterm_modifiers_on_navigation_keys(token: str, expected: str) -> None:
    assert _keys(token) == expected


def test_unknown_key_with_modifiers_warns_and_sends_literal() -> None:
    result = encode_key_sequence(KeyEncodingRequest(keys=["C-foobar"]))

    assert result.data == "foobar"
    assert len(result.warnings) == 1
    assert "foobar" in result.warnings[0]


@pytest.mark.parametrize(
    "hex_bytes,expected",
    [
        (["0x0d", "1b", "a", "ff", "00", "  0x0a  "], "\r\x1b\n\xff\x00\n"),
        (["0x0d", "ZZ", "0x123", ""], "\r"),
    ],
)
def test_hex_bytes(hex_bytes: list[str], expected: str) -> None:
    result = encode_key_sequence(KeyEncodingRequest(hex_bytes=hex_bytes))

    assert result.data == expected
    invalid = [raw for raw in hex_bytes if raw in ("ZZ", "0x123", "")]
    assert result.warnings == [f"Invalid hex byte: {raw}" for raw in invalid]


def test_literal_then_hex_then_keys() -> None:
    request = KeyEncodingRequest(literal="a", hex_bytes=["0x0d"], keys=["tab", "^C"])

    assert encode_key_sequence(request).data == "a\r\t\x03"


def test_empty_request() -> None:
    result = encode_key_sequence(KeyEncodingRequest())

    assert (result.data, result.warnings) == ("", [])


def test_paste() -> None:
    assert encode_paste("a\nb") == f"{ESC}[200~a\nb{ESC}[201~"
    assert encode_paste("a\nb", bracketed=False) == "a\nb"
