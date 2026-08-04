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

_HEREDOC_START = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


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


def _find_comment_start(line: str, in_heredoc: bool) -> Optional[int]:
    """Index of an unquoted `#` that opens a comment, else None.

    Mirrors POSIX word-splitting closely enough to be safe: a `#` only starts a
    comment at the beginning of a word (start of line or after whitespace) and
    outside single quotes, double quotes, and backslash escapes.
    """
    if in_heredoc:
        return None

    single = False
    double = False
    prev_was_space = True

    i = 0
    while i < len(line):
        ch = line[i]

        if ch == "\\" and not single:
            i += 2
            prev_was_space = False
            continue

        if ch == "'" and not double:
            single = not single
        elif ch == '"' and not single:
            double = not double
        elif ch == "#" and not single and not double and prev_was_space:
            return i

        prev_was_space = ch.isspace()
        i += 1

    return None


def _scan_heredoc_delimiters(line: str) -> List[str]:
    """Heredoc delimiters opened by this line, in order."""
    return [m.group(2) for m in _HEREDOC_START.finditer(line)]


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
    pending_heredocs: List[str] = []

    for line in lines:
        in_heredoc = bool(pending_heredocs)

        if in_heredoc:
            # Inside a heredoc body every byte is data — never touch it, and
            # only look for the closing delimiter.
            if line.strip() == pending_heredocs[0]:
                pending_heredocs.pop(0)
            out_lines.append(line)
            continue

        idx = _find_comment_start(line, in_heredoc=False)
        if idx is None:
            pending_heredocs.extend(_scan_heredoc_delimiters(line))
            out_lines.append(line)
            continue

        code, comment = line[:idx], line[idx:]
        if not _is_narration(comment):
            pending_heredocs.extend(_scan_heredoc_delimiters(line))
            out_lines.append(line)
            continue

        stripped.append(comment.lstrip("#").strip())
        remainder = code.rstrip()
        pending_heredocs.extend(_scan_heredoc_delimiters(remainder))
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
