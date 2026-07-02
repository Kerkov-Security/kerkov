import asyncio
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

MAX_PROMPT_BYTES = 100 * 1024

_HEADER = (
    "# Type your prompt below. Lines starting with '#' are ignored.\n"
    "# Save and close the editor to send. Empty content cancels.\n"
)


class LoadPromptFromFileResult(BaseModel):
    success: bool
    prompt: Optional[str] = None
    errorMessage: Optional[str] = None


class ComposePromptResult(BaseModel):
    success: bool
    errorMessage: Optional[str] = None
    prompt: Optional[str] = None


def _resolve_editor() -> list[str]:
    posix = sys.platform != "win32"
    for env_var in ("VISUAL", "EDITOR"):
        value = os.environ.get(env_var)
        if value:
            return shlex.split(value, posix=posix)

    for candidate in ("vim", "nano"):
        if shutil.which(candidate):
            return [candidate]

    if sys.platform == "win32":
        return ["notepad"]
    return ["vi"]


def _strip_comments(text: str) -> str:
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    return "\n".join(lines).strip()


class ExternalPromptDelegate:

    def load_prompt_from_file(self, raw_path: str) -> LoadPromptFromFileResult:
        path = Path(raw_path).expanduser()

        if not path.is_file():
            return LoadPromptFromFileResult(
                success=False,
                errorMessage=f"Not a file: {path}",
            )

        size = path.stat().st_size
        if size > MAX_PROMPT_BYTES:
            return LoadPromptFromFileResult(
                success=False,
                errorMessage=f"File too large ({size} bytes, max {MAX_PROMPT_BYTES}).",
            )

        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return LoadPromptFromFileResult(
                success=False,
                errorMessage="File is not valid UTF-8 text.",
            )

        content = content.strip()
        if not content:
            return LoadPromptFromFileResult(
                success=False,
                errorMessage="File is empty — cancelled.",
            )

        return LoadPromptFromFileResult(
            success=True,
            prompt=content,
        )

    async def compose_prompt(self) -> ComposePromptResult:
        editor_cmd = _resolve_editor()

        fd, tmp_path = tempfile.mkstemp(prefix="kerkov-prompt-", suffix=".md")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(_HEADER)

            try:
                await asyncio.to_thread(
                    subprocess.run, [*editor_cmd, tmp_path], check=True
                )
            except FileNotFoundError:
                return ComposePromptResult(
                    success=False,
                    errorMessage=f"Editor not found: {editor_cmd[0]}. " + f"Set $EDITOR or $VISUAL."
                )
            except subprocess.CalledProcessError as exc:
                return ComposePromptResult(
                    success=False,
                    errorMessage=f"Editor exited with error: {exc}"
                )

            size = os.path.getsize(tmp_path)
            if size > MAX_PROMPT_BYTES:
                return ComposePromptResult(
                    success=False,
                    errorMessage=f"Prompt too large ({size} bytes, max {MAX_PROMPT_BYTES}).",
                )

            with open(tmp_path, "r", encoding="utf-8") as f:
                content = _strip_comments(f.read())

            return ComposePromptResult(
                success=True,
                prompt=content
            )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

