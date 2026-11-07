import pytest

from kerkov.core.process.ansi_strip import strip_ansi


@pytest.mark.parametrize(
    "raw,expected",
    [
        pytest.param("hello world", "hello world", id="plain"),
        pytest.param("line1\n\tline2\r\n", "line1\n\tline2\r\n", id="whitespace-kept"),
        pytest.param("\x1b[1;31;42mred\x1b[0m", "red", id="sgr"),
        pytest.param("\x1b[38;2;255;0;0mred\x1b[m", "red", id="sgr-truecolor"),
        pytest.param("\x1b[5Aup\x1b[2K", "up", id="cursor-and-erase"),
        pytest.param("\x1b[?25lhidden\x1b[?25h", "hidden", id="private-mode"),
        pytest.param("\x1b]0;title\x07text", "text", id="osc-bel"),
        pytest.param("\x1b]0;title\x1b\\text", "text", id="osc-st"),
        pytest.param("\x1bOPa\x1bNAb", "ab", id="ss3-ss2"),
        pytest.param("\x1b=a\x1b>b", "ab", id="keypad-mode"),
        # `tput sgr0` emits ESC ( B before the SGR reset.
        pytest.param("\x1b(B\x1b[mtext", "text", id="charset-sgr0"),
        pytest.param("\x1b)0line\x1b(0q\x1b(B", "lineq", id="charset-g0-g1"),
        pytest.param("50%\r100%", "50%100%", id="progress-overwrite"),
        pytest.param("a\x00b\x08c\x07d\x7fe", "abcde", id="control-chars"),
        pytest.param(
            "\x1b[01;34mdir\x1b[0m\n\x1b]0;t\x07\x1b[32mfile\x1b[0m\n",
            "dir\nfile\n",
            id="multiline-mixed",
        ),
    ],
)
def test_strip_ansi(raw: str, expected: str) -> None:
    assert strip_ansi(raw) == expected
