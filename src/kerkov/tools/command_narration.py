"""
Strip LLM narration comments out of shell commands.

Models sometimes explain themselves inside the `command` they pass to the exec
tool (`# first, let's see what's listening`) instead of in the assistant text the
conversation engine already surfaces. The comment then runs through the shell,
clutters the transcript, and buries the actual command.

`strip_narration_comments` removes those comments and reports how many it took,
so the caller can attach a one-line nudge to the ToolResult. It is deliberately
conservative: a `#` is only treated as a comment when the scanner knows it sits
outside every quoting and heredoc context, so load-bearing `#` characters
(`curl 'http://h/#frag'`, `sed 's/#//'`, `awk '{print $1}  # not a comment'`
inside quotes) survive untouched.

The scanner carries quote and heredoc state across newlines, because a command
is one shell script and not a list of independent lines: a quote opened on line
one is still open on line two, and a heredoc body ends only on a line the shell
itself would accept as the terminator. Anything the scanner cannot parse with
confidence (an unterminated quote, a heredoc operator in a shape it does not
recognise) stops the stripping instead of guessing — a missed narration comment
costs a line of transcript noise, a wrong strip corrupts a command.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

# A comment is narration when it reads like speech rather than an annotation.
# Requiring a first-person/plan-shaped opener keeps genuine operator notes
# (`# nosec`, `# noqa`, `# TODO: retry`, `# 3 retries`) in the command.
_NARRATION_OPENERS = re.compile(
    r"""^(?:
        (?:i|we)(?:'|\s|$)          # I'll, I will, we can, I'm
        |(?:let|lets|let's)\s
        |(?:now|next|then|first|finally|afterwards?)\b
        |(?:start|starting|begin|beginning)\s+(?:by|with)\b
        |(?:this|that|it)\s+(?:will|should|is\s+going)\b
        |(?:check|checking|see|seeing|look|looking|try|trying
          |scan|scanning|enumerate|enumerating|grab|grabbing)\s
        |(?:step\s+\d+\b)
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Characters that end an unquoted word, and so end a bare heredoc delimiter.
_WORD_TERMINATORS = frozenset(" \t;&|<>()")


@dataclass(frozen=True)
class _Heredoc:
    """A heredoc body the scanner is waiting to see the end of."""

    delimiter: str
    strip_tabs: bool  # `<<-` lets the terminator be indented with tabs

    def closes(self, line: str) -> bool:
        """True when the shell would treat `line` as this heredoc's terminator.

        The match is exact: `<<EOF` is closed only by a line that is exactly
        `EOF`, not by `  EOF` or `EOF `. Only `<<-` permits leading tabs, and
        tabs alone -- not spaces.
        """
        candidate = line.lstrip("\t") if self.strip_tabs else line
        return candidate == self.delimiter


@dataclass
class _LineScan:
    """What one line does to the scanner's state."""

    comment_index: Optional[int]
    heredocs: List[_Heredoc]
    single: bool
    double: bool
    unparsable: bool = False


@dataclass
class NarrationStripResult:
    """Outcome of scanning one command for narration comments."""

    command: str
    stripped: List[str]

    @property
    def was_stripped(self) -> bool:
        return bool(self.stripped)

    def nudge(self) -> Optional[str]:
        """One-line correction to hand back to the model, or None."""
        if not self.stripped:
            return None
        count = len(self.stripped)
        noun = "comment" if count == 1 else "comments"
        return (
            f"Note: stripped {count} narration {noun} from `command` before running "
            f"it ({_quote_first(self.stripped)}). Commentary belongs in your reply "
            f"text, which the user already sees — keep `command` executable only."
        )


def _quote_first(stripped: List[str]) -> str:
    first = stripped[0]
    if len(first) > 60:
        first = first[:57] + "..."
    extra = f", +{len(stripped) - 1} more" if len(stripped) > 1 else ""
    return f'"{first}"{extra}'


def _read_heredoc_delimiter(line: str, start: int) -> Tuple[Optional[str], int]:
    """Read the delimiter word beginning at `start`, with its quoting removed.

    Handles every shape the shell accepts -- `EOF`, `'END-MARKER'`, `"EOF"`,
    `\\EOF`, and mixtures like `E'OF'` -- and returns the delimiter as the shell
    would compare it, i.e. with the quotes taken off. Returns `(None, start)`
    when the word cannot be read (end of line, an unterminated quote), which the
    caller treats as a reason to stop stripping.
    """
    parts: List[str] = []
    i = start

    while i < len(line):
        ch = line[i]

        if ch in _WORD_TERMINATORS:
            break

        if ch == "\\":
            if i + 1 >= len(line):
                return None, start
            parts.append(line[i + 1])
            i += 2
            continue

        if ch in ("'", '"'):
            close = line.find(ch, i + 1)
            if close == -1:
                return None, start
            parts.append(line[i + 1 : close])
            i = close + 1
            continue

        parts.append(ch)
        i += 1

    delimiter = "".join(parts)
    if not delimiter:
        return None, start
    return delimiter, i


def _scan_line(line: str, single: bool, double: bool) -> _LineScan:
    """Walk one line, carrying quote state in and out.

    Reports the index of an unquoted `#` that opens a comment, the heredocs the
    line opens ahead of that comment, and the quote state the next line inherits.
    A `#` only starts a comment at the beginning of a word (start of line or
    after whitespace) and outside single quotes, double quotes, and backslash
    escapes.
    """
    heredocs: List[_Heredoc] = []
    prev_was_space = True
    i = 0

    while i < len(line):
        ch = line[i]

        if ch == "\\" and not single:
            # A trailing backslash continues the line; the newline is consumed
            # rather than starting anything, so state simply carries over.
            i += 2
            prev_was_space = False
            continue

        if ch == "'" and not double:
            single = not single
        elif ch == '"' and not single:
            double = not double
        elif not single and not double:
            if ch == "#" and prev_was_space:
                # The rest of the line is comment text, so it opens no heredocs
                # and leaves the next line unquoted.
                return _LineScan(
                    comment_index=i, heredocs=heredocs, single=False, double=False
                )
            if line.startswith("<<", i):
                if line.startswith("<<<", i):
                    # A here-string has no body to protect.
                    i += 3
                    prev_was_space = False
                    continue
                j = i + 2
                strip_tabs = line.startswith("-", j)
                if strip_tabs:
                    j += 1
                while j < len(line) and line[j] in " \t":
                    j += 1
                delimiter, end = _read_heredoc_delimiter(line, j)
                if delimiter is None:
                    return _LineScan(
                        comment_index=None,
                        heredocs=[],
                        single=single,
                        double=double,
                        unparsable=True,
                    )
                heredocs.append(_Heredoc(delimiter=delimiter, strip_tabs=strip_tabs))
                i = end
                prev_was_space = False
                continue

        prev_was_space = ch.isspace()
        i += 1

    return _LineScan(
        comment_index=None, heredocs=heredocs, single=single, double=double
    )


def _is_narration(comment_body: str) -> bool:
    body = comment_body.strip().lstrip("#").strip()
    if not body:
        return False
    return bool(_NARRATION_OPENERS.match(body))


def strip_narration_comments(command: str) -> NarrationStripResult:
    """Remove narration-style shell comments from `command`.

    Returns the cleaned command plus the comment bodies that were removed. When
    nothing looks like narration the original string is returned unchanged, so
    this is safe to call on every exec.
    """
    lines = command.split("\n")
    out_lines: List[str] = []
    stripped: List[str] = []
    pending_heredocs: List[_Heredoc] = []
    single = False
    double = False

    for line in lines:
        if pending_heredocs:
            # Inside a heredoc body every byte is data — never touch it, and
            # only look for the closing delimiter.
            if pending_heredocs[0].closes(line):
                pending_heredocs.pop(0)
            out_lines.append(line)
            continue

        scan = _scan_line(line, single, double)
        if scan.unparsable:
            # The scanner lost the thread. Hand back what the model sent rather
            # than edit a command we no longer understand.
            return NarrationStripResult(command=command, stripped=[])

        single, double = scan.single, scan.double
        idx = scan.comment_index

        if idx is None or not _is_narration(line[idx:]):
            pending_heredocs.extend(scan.heredocs)
            out_lines.append(line)
            continue

        stripped.append(line[idx:].lstrip("#").strip())
        remainder = line[:idx].rstrip()
        # `scan.heredocs` stops at the comment, so these are the heredocs opened
        # by the code that survives.
        pending_heredocs.extend(scan.heredocs)
        if remainder:
            out_lines.append(remainder)
        # A whole-line comment leaves nothing behind; drop the line entirely so
        # the command doesn't grow blank gaps.

    if not stripped:
        return NarrationStripResult(command=command, stripped=[])

    cleaned = "\n".join(out_lines).strip()
    if not cleaned:
        # The model sent nothing but narration. Hand the original back and let
        # validate_params/the shell surface the real problem.
        return NarrationStripResult(command=command, stripped=[])

    return NarrationStripResult(command=cleaned, stripped=stripped)
