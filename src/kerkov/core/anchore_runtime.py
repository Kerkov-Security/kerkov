"""Resolve preinstalled Syft and Grype command-line tools."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol

SUPPORTED_TOOLS = frozenset({"syft", "grype"})


class AnchoreRuntimeError(RuntimeError):
    """A safe, user-facing failure while resolving an Anchore tool."""


@dataclass(frozen=True)
class AnchoreRuntimeInfo:
    """The executable selected for a Syft or Grype operation."""

    binary: str
    source: str
    version: Optional[str]


class AnchoreRuntimeResolver(Protocol):
    """Injectable runtime resolver used by the supply-chain tools."""

    async def resolve(self) -> AnchoreRuntimeInfo:
        """Return a usable executable."""
        ...


BinaryLocator = Callable[[str], Optional[str]]


class PathAnchoreRuntime:
    """Resolve a Syft or Grype executable already available on PATH."""

    def __init__(
        self,
        tool: str,
        *,
        locator: BinaryLocator = shutil.which,
    ) -> None:
        if tool not in SUPPORTED_TOOLS:
            raise ValueError(f"unsupported Anchore tool: {tool!r}")
        self.tool = tool
        self._locator = locator

    async def resolve(self) -> AnchoreRuntimeInfo:
        try:
            located = self._locator(self.tool)
        except OSError as exc:
            raise AnchoreRuntimeError(
                f"could not search PATH for {self.tool}: {exc}"
            ) from exc
        if not located:
            raise AnchoreRuntimeError(
                f"no {self.tool} binary was found on PATH. Install {self.tool} "
                "before running this tool"
            )

        try:
            binary = Path(located).expanduser().resolve()
            is_file = binary.is_file()
        except OSError as exc:
            raise AnchoreRuntimeError(
                f"could not inspect the {self.tool} executable on PATH: {exc}"
            ) from exc
        if not is_file:
            raise AnchoreRuntimeError(
                f"the {self.tool} executable resolved from PATH is not a file"
            )
        return AnchoreRuntimeInfo(
            binary=str(binary),
            source="system",
            version=None,
        )
