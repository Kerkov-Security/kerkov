"""How an approved tool call generalizes into a saved approval rule."""

import re
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, Mapping, Optional, Sequence
from urllib.parse import SplitResult, urlsplit

from pydantic import BaseModel

from kerkov.core.run_workspace import resolve_file_path


def escape_glob_literal(value: Any) -> str:
    """Escape glob metacharacters so a captured value matches only itself."""
    return re.sub(r"([*?\[\]])", r"[\1]", str(value))


def split_approval_url(url: str) -> SplitResult:
    """Parse an HTTP URL with a credential-free authority for rule matching.

    Invalid URLs raise ValueError; callers must not offer saved grants for them.
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("Expected an HTTP or HTTPS URL with a host")
    host = parts.hostname
    netloc = f"[{host}]" if ":" in host else host
    if parts.port is not None:
        netloc += f":{parts.port}"
    return parts._replace(netloc=netloc)


class ApprovalScope(BaseModel):
    """How far one approval may be generalized when the operator saves it.

    ``broad_params`` is the widest pattern the tool is willing to have granted
    by a single keystroke: ``None`` offers no broad rung at all, ``{}`` offers
    the whole tool. ``volatile_params`` never reach a saved rule in either form.
    ``exact_overrides`` replaces a parameter's value in the *exact* rule, for
    values that identify the call but must not be written down verbatim -- a URL
    carrying userinfo credentials is still the identity of the request, so it
    cannot simply be dropped. Override values are patterns: escape them.
    """

    broad_params: Optional[Dict[str, str]] = None
    volatile_params: FrozenSet[str] = frozenset()
    exact_overrides: Dict[str, str] = {}
    # Disable saved grants for invalid targets or mutable executable inputs.
    saveable: bool = True

    @classmethod
    def exact_only(
        cls,
        *,
        volatile: Iterable[str] = (),
        exact_overrides: Optional[Mapping[str, str]] = None,
    ) -> "ApprovalScope":
        """Offer no broad rung: this call's arguments have no defensible boundary."""
        return cls(
            broad_params=None,
            volatile_params=frozenset(volatile),
            exact_overrides=dict(exact_overrides or {}),
        )

    @classmethod
    def whole_tool(
        cls,
        *,
        volatile: Iterable[str] = (),
        exact_overrides: Optional[Mapping[str, str]] = None,
    ) -> "ApprovalScope":
        """Offer the whole tool: nothing below it is worth distinguishing."""
        return cls(
            broad_params={},
            volatile_params=frozenset(volatile),
            exact_overrides=dict(exact_overrides or {}),
        )

    @classmethod
    def matching(
        cls,
        patterns: Mapping[str, str],
        *,
        volatile: Iterable[str] = (),
        exact_overrides: Optional[Mapping[str, str]] = None,
    ) -> "ApprovalScope":
        """Offer an explicit pattern; literal segments must already be escaped."""
        return cls(
            broad_params=dict(patterns),
            volatile_params=frozenset(volatile),
            exact_overrides=dict(exact_overrides or {}),
        )

    @classmethod
    def keeping(
        cls,
        params: Mapping[str, Any],
        fields: Sequence[str],
        *,
        volatile: Iterable[str] = (),
    ) -> "ApprovalScope":
        """Offer the named discriminating fields, dropping every other argument."""
        kept = {f: escape_glob_literal(params[f]) for f in fields if f in params}
        if not kept:
            return cls.exact_only(volatile=volatile)
        return cls.matching(kept, volatile=volatile)

    @classmethod
    def parent_directory(
        cls,
        params: Mapping[str, Any],
        field: str,
        *,
        volatile: Iterable[str] = (),
    ) -> "ApprovalScope":
        """Offer the directory containing a path argument."""
        raw = params.get(field)
        if not raw:
            return cls.exact_only(volatile=volatile)
        try:
            target = resolve_file_path(str(raw))
        except (OSError, RuntimeError, ValueError):
            return cls(saveable=False)
        overrides = {field: escape_glob_literal(target.as_posix())}
        if Path(str(raw)).parent == Path("."):
            return cls.exact_only(volatile=volatile, exact_overrides=overrides)
        return cls.matching(
            {field: escape_glob_literal(target.parent.as_posix().rstrip("/")) + "/*"},
            volatile=volatile,
            exact_overrides=overrides,
        )
