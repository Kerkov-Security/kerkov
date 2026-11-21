"""Answer an MCP server's mid-call request for input (elicitation).

A server may pause a tool call to ask the operator for input: a short form
(`form` mode) or a page to visit (`url` mode). The request reaches whoever is
driving the call through a context variable each front end sets around its own
turn. Delegated agents clear it, so their calls are refused rather than left
waiting on a prompt nobody is watching.

Only the operator's answer goes back to the server. It is never written to the
conversation, the transcript or the log.
"""

from __future__ import annotations

import urllib.parse
from abc import ABC, abstractmethod
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Literal, Mapping, Optional, Tuple

from jsonschema import Draft202012Validator

from kerkov.core.logging.logger_factory import get_logger

logger = get_logger("McpElicitation")

Action = Literal["accept", "decline", "cancel"]
FieldKind = Literal["string", "number", "integer", "boolean", "choice", "choices"]

# An error the SDK raises from the call; `McpConnection` names it for the model.
NOBODY_TO_ASK = "Elicitation unanswered: nobody can answer this call"
UNSUPPORTED_FORM = "Elicitation unanswered: the form uses unsupported fields"
_INVALID_PARAMS = -32602


@dataclass(frozen=True)
class ElicitationField:
    """One top-level property of a form request, as the operator answers it."""

    name: str
    kind: FieldKind
    title: str = ""
    description: str = ""
    required: bool = False
    choices: Tuple[str, ...] = ()
    choice_labels: Tuple[str, ...] = ()
    default: Any = None

    @property
    def label(self) -> str:
        return self.title or self.name


@dataclass(frozen=True)
class ElicitationRequest:
    server: str
    mode: Literal["form", "url"]
    message: str
    fields: Tuple[ElicitationField, ...] = ()
    schema: Mapping[str, Any] = field(default_factory=dict)
    url: Optional[str] = None

    @property
    def url_origin(self) -> str:
        parts = urllib.parse.urlsplit(self.url or "")
        return f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}"


@dataclass(frozen=True)
class ElicitationResponse:
    action: Action
    content: Optional[Dict[str, Any]] = None


class Elicitor(ABC):
    """A front end's way to put one request in front of the operator."""

    @abstractmethod
    async def elicit(self, request: ElicitationRequest) -> ElicitationResponse:
        """Ask, and return the operator's answer; `cancel` if they dismiss it."""


_current: ContextVar[Optional[Elicitor]] = ContextVar("mcp_elicitor", default=None)


@contextmanager
def answered_by(elicitor: Optional[Elicitor]) -> Iterator[None]:
    """Route elicitation from calls made in this context to `elicitor`."""
    token = _current.set(elicitor)
    try:
        yield
    finally:
        _current.reset(token)


def unanswerable() -> None:
    """Refuse elicitation for the rest of this task, as an agent's run does."""
    _current.set(None)


def parse_form_schema(schema: Mapping[str, Any]) -> Optional[List[ElicitationField]]:
    """The form's fields, or None when it uses something outside the spec's subset."""
    properties = schema.get("properties")
    if schema.get("type", "object") != "object" or not isinstance(properties, dict):
        return None
    required = set(schema.get("required") or ())
    fields: List[ElicitationField] = []
    for name, spec in properties.items():
        parsed = _parse_field(str(name), spec, str(name) in required)
        if parsed is None:
            return None
        fields.append(parsed)
    return fields


def _parse_field(name: str, spec: Any, required: bool) -> Optional[ElicitationField]:
    if not isinstance(spec, dict):
        return None
    common = {
        "name": name,
        "title": str(spec.get("title") or ""),
        "description": " ".join(str(spec.get("description") or "").split()),
        "required": required,
        "default": spec.get("default"),
    }
    kind = spec.get("type")
    choices, labels = _choices(spec)
    if kind == "string" and choices:
        return ElicitationField(
            kind="choice", choices=choices, choice_labels=labels, **common
        )
    if kind == "array":
        items = spec.get("items")
        choices, labels = _choices(items) if isinstance(items, dict) else ((), ())
        if not choices:
            return None
        return ElicitationField(
            kind="choices", choices=choices, choice_labels=labels, **common
        )
    if kind in ("string", "number", "integer", "boolean"):
        return ElicitationField(kind=kind, **common)
    return None


def _choices(spec: Mapping[str, Any]) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """`enum` (with optional `enumNames`), or `oneOf`/`anyOf` of titled consts."""
    if isinstance(spec.get("enum"), list):
        values = tuple(str(value) for value in spec["enum"])
        names = spec.get("enumNames")
        labels = (
            tuple(str(n) for n in names)
            if isinstance(names, list) and len(names) == len(values)
            else values
        )
        return values, labels
    options = spec.get("oneOf") or spec.get("anyOf")
    if isinstance(options, list) and all(
        isinstance(o, dict) and "const" in o for o in options
    ):
        values = tuple(str(o["const"]) for o in options)
        return values, tuple(str(o.get("title") or o["const"]) for o in options)
    return (), ()


def coerce_answer(item: ElicitationField, text: str) -> Tuple[Any, Optional[str]]:
    """A typed value from what the operator typed, or why it is not acceptable."""
    text = text.strip()
    if not text:
        if item.required:
            return None, f"{item.label} is required"
        return None, None
    if item.kind == "boolean":
        if text.lower() in ("y", "yes", "true", "1"):
            return True, None
        if text.lower() in ("n", "no", "false", "0"):
            return False, None
        return None, "answer yes or no"
    if item.kind == "integer":
        try:
            return int(text), None
        except ValueError:
            return None, "enter a whole number"
    if item.kind == "number":
        try:
            return float(text), None
        except ValueError:
            return None, "enter a number"
    if item.kind == "choice":
        return _pick(item, text)
    if item.kind == "choices":
        picked = []
        for part in text.split(","):
            value, error = _pick(item, part.strip())
            if error:
                return None, error
            picked.append(value)
        return picked, None
    return text, None


def _pick(item: ElicitationField, text: str) -> Tuple[Any, Optional[str]]:
    if text.isdigit() and 1 <= int(text) <= len(item.choices):
        return item.choices[int(text) - 1], None
    for value, label in zip(item.choices, item.choice_labels):
        if text in (value, label):
            return value, None
    return None, "pick one of the listed options"


def check_content(
    schema: Mapping[str, Any], content: Mapping[str, Any]
) -> Optional[str]:
    """Why `content` does not satisfy the request's schema, naming no value."""
    error = next(iter(Draft202012Validator(schema).iter_errors(dict(content))), None)
    if error is None:
        return None
    where = "/".join(str(part) for part in error.absolute_path) or "form"
    return f"{where}: fails {error.validator!r}"


def elicitation_callback(server_key: str) -> Any:
    """The SDK `elicitation_callback` for one server's client."""

    async def callback(context: Any, params: Any) -> Any:
        import mcp_types as types

        elicitor = _current.get()
        if elicitor is None:
            return types.ErrorData(code=_INVALID_PARAMS, message=NOBODY_TO_ASK)
        if getattr(params, "mode", "form") == "url":
            request = ElicitationRequest(
                server=server_key,
                mode="url",
                message=params.message,
                url=params.url,
            )
        else:
            schema = dict(params.requested_schema or {})
            fields = parse_form_schema(schema)
            if fields is None:
                logger.warning(
                    "MCP server %s: unsupported elicitation form", server_key
                )
                return types.ErrorData(code=_INVALID_PARAMS, message=UNSUPPORTED_FORM)
            request = ElicitationRequest(
                server=server_key,
                mode="form",
                message=params.message,
                fields=tuple(fields),
                schema=schema,
            )
        response = await elicitor.elicit(request)
        logger.info(
            "MCP server %s: elicitation answered with %s", server_key, response.action
        )
        if response.action != "accept" or request.mode == "url":
            return types.ElicitResult(action=response.action)
        return types.ElicitResult(action="accept", content=response.content or {})

    return callback
