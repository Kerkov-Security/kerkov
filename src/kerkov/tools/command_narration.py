"""
Notice when a model narrates inside the exec `command` instead of in its reply.

Models sometimes explain themselves in the command they pass to the exec tool
(`# first, let's see what's listening`) rather than in the assistant text the
conversation engine already surfaces.

The command is never edited. `bash -c` discards comments before running
anything, so stripping them would buy nothing at runtime while putting a
half-implemented shell parser between the model and execution. Detection can
therefore stay simple: a wrong guess here costs one line of metadata, never a
command.
"""

from __future__ import annotations

import re
from typing import List, Optional

_COMMENT_LINE = re.compile(r"^\s*#(.*)$")

# Comments some other tool reads, not the model talking to itself. A closed set
# of known tokens, unlike the open-ended problem of recognising prose.
_TOOL_DIRECTIVES = re.compile(
    r"""^(?:
        !                           # shebang
        |nosec\b                    # bandit
        |noqa\b                     # flake8, ruff
        |type:\s                    # mypy
        |(?:mypy|ruff|flake8|pylint|pyright)\b\s*:
        |shellcheck\b
        |hadolint\b
        |fmt:\s                     # gofmt, black
        |pragma:\s                  # coverage
    )""",
    re.IGNORECASE | re.VERBOSE,
)


def _is_tool_directive(body: str) -> bool:
    """True when the comment is aimed at another tool, not at the reader."""
    return bool(_TOOL_DIRECTIVES.match(body))


def find_narration_comments(command: str) -> List[str]:
    """Comment lines that read as the model narrating.

    Only whole-line comments count. Trailing comments are missed and heredoc
    bodies are not excluded, both on purpose: this decides whether to add a
    note, so precision is worth less than keeping a shell parser out of here.
    """
    found: List[str] = []
    for line in command.split("\n"):
        match = _COMMENT_LINE.match(line)
        if match is None:
            continue
        body = match.group(1).strip()
        if body and not _is_tool_directive(body):
            found.append(body)
    return found


def narration_nudge(command: str) -> Optional[str]:
    """One-line correction to hand back to the model, or None."""
    narration = find_narration_comments(command)
    if not narration:
        return None
    count = len(narration)
    noun = "comment" if count == 1 else "comments"
    return (
        f"Note: `command` carried {count} narration {noun} "
        f"({_quote_first(narration)}). It ran as sent — the shell ignores "
        f"comments — but commentary belongs in your reply text, which the user "
        f"already sees."
    )


def _quote_first(narration: List[str]) -> str:
    first = narration[0]
    if len(first) > 60:
        first = first[:57] + "..."
    extra = f", +{len(narration) - 1} more" if len(narration) > 1 else ""
    return f'"{first}"{extra}'
