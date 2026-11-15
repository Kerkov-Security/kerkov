"""A Kerkov tool backed by one tool discovered on an MCP server."""

from __future__ import annotations

import copy
from typing import Any, Awaitable, Callable, Dict, Optional, Type

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError, best_match
from jsonschema.validators import validator_for
from pydantic import ConfigDict

from kerkov.core.approval_scope import ApprovalScope
from kerkov.tools.base_tool import BaseTool, GenericToolSchema, ToolParams, ToolResult
from kerkov.tools.mcp.results import adapt_call_result
from kerkov.tools.schema import normalize_schema

McpCaller = Callable[[str, str, Dict[str, Any], float], Awaitable[Any]]


class McpToolParams(ToolParams):
    """Arguments pass through untouched; the server's own schema validates them."""

    model_config = ConfigDict(extra="allow")


class SchemaNotRepresentableError(ValueError):
    """The server's input schema cannot be advertised to the model."""


class McpTool(BaseTool):
    """Advertises a server's schema verbatim and forwards calls to its connection.

    Descriptions, schemas and results come from the server and are untrusted
    data. Tool annotations such as `readOnlyHint` are deliberately ignored: the
    specification marks them untrusted, so they must not shape approval.
    """

    prompts_every_call = True

    def __init__(
        self,
        *,
        name: str,
        server_key: str,
        original_name: str,
        description: str,
        input_schema: Dict[str, Any],
        caller: McpCaller,
        timeout_seconds: float,
    ) -> None:
        super().__init__(
            name=name,
            description=f"[MCP server '{server_key}' tool '{original_name}'] "
            + (description.strip() or "No description provided."),
        )
        self.server_key = server_key
        self.original_name = original_name
        self.timeout_seconds = timeout_seconds
        self._caller = caller
        self._validator = _validator(input_schema)
        try:
            self._schema = normalize_schema(input_schema)
        except ValueError as exc:
            raise SchemaNotRepresentableError(str(exc)) from exc

    def get_params_class(self) -> Type[ToolParams]:
        return McpToolParams

    def get_generic_schema(self) -> GenericToolSchema:
        return GenericToolSchema(
            name=self.name,
            description=self.description,
            parameters=copy.deepcopy(self._schema),
        )

    def approval_scope(self, params: Dict[str, Any]) -> ApprovalScope:
        # One adapter stands in for unknown tools, so no boundary is defensible,
        # and a schema may name secret arguments that a saved rule would write
        # verbatim into approvals.yaml. Every call is approved on its own.
        return ApprovalScope(saveable=False)

    def validate_params(self, toolParams: ToolParams) -> Optional[str]:
        error = best_match(self._validator.iter_errors(_arguments(toolParams)))
        if error is None:
            return None
        location = "/".join(str(part) for part in error.absolute_path)
        detail = _describe_violation(error)
        return f"{location}: {detail}" if location else detail

    def loggable_params(self, dict_params: Dict[str, Any]) -> Any:
        # A server's schema may carry tokens or passwords; log only the keys.
        return sorted(dict_params)

    async def run(self, toolParams: ToolParams) -> ToolResult:
        from kerkov.core.mcp.connection import McpCallError

        try:
            result = await self._caller(
                self.server_key,
                self.original_name,
                _arguments(toolParams),
                self.timeout_seconds,
            )
        except McpCallError as exc:
            return ToolResult(success=False, output="", error=str(exc))
        return adapt_call_result(
            result,
            server_key=self.server_key,
            tool_name=self.original_name,
            call_id=self.execution_call_id,
        )


# Messages for these keywords name properties, never the submitted values.
_NAME_ONLY_KEYWORDS = frozenset({"required", "additionalProperties"})
_CONSTRAINT_PREVIEW_CHARS = 120


def _describe_violation(error: ValidationError) -> str:
    """The failed keyword and its constraint; `error.message` quotes the input."""
    if error.validator in _NAME_ONLY_KEYWORDS:
        return error.message
    constraint = repr(error.validator_value)
    if len(constraint) > _CONSTRAINT_PREVIEW_CHARS:
        constraint = constraint[: _CONSTRAINT_PREVIEW_CHARS - 3] + "..."
    return f"value fails {error.validator!r} {constraint}"


def _arguments(toolParams: ToolParams) -> Dict[str, Any]:
    return dict(toolParams.model_extra or {})


def _validator(schema: Dict[str, Any]) -> Any:
    try:
        cls = validator_for(schema, default=Draft202012Validator)
        cls.check_schema(schema)
    except Exception as exc:
        raise SchemaNotRepresentableError(f"invalid input schema: {exc}") from exc
    return cls(schema)
