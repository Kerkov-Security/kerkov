"""Read an MCP server's resources: one tool per server that offers them."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Literal, Optional, Type

from pydantic import Field

from kerkov.core.approval_scope import ApprovalScope
from kerkov.tools.base_tool import ToolParams, ToolResult
from kerkov.tools.mcp.mcp_tool import McpServerTool
from kerkov.tools.mcp.results import adapt_read_result, adapt_resource_listing

ResourceAction = Literal["list", "templates", "read"]
McpResourceRequest = Callable[[str, str, Optional[str], float], Awaitable[Any]]

RESOURCE_TOOL_SUFFIX = "resources"


class McpResourceParams(ToolParams):
    action: ResourceAction = Field(
        description="list: the resources the server offers. templates: its URI "
        "templates, to fill in and read. read: one resource's contents."
    )
    uri: Optional[str] = Field(
        default=None,
        description="The resource URI to read; required for read, unused otherwise.",
    )
    cursor: Optional[str] = Field(
        default=None,
        description="Continue a list or templates listing from a previous page.",
    )


class McpResourceTool(McpServerTool):
    """Lists and reads one server's resources, which are data, not actions.

    Resource contents come from the server and are untrusted data, like tool
    results. The description is fixed so it never moves the cached prefix.
    """

    def __init__(
        self,
        *,
        name: str,
        server_key: str,
        request: McpResourceRequest,
        timeout_seconds: float,
    ) -> None:
        super().__init__(
            name=name,
            description=(
                f"[MCP server '{server_key}' resources] List the resources and "
                "URI templates this MCP server offers, or read one resource by "
                "URI. Resources are read-only context such as files, records or "
                "documents; a tool result's resource link can be read here too."
            ),
        )
        self.server_key = server_key
        self.original_name = RESOURCE_TOOL_SUFFIX
        self.timeout_seconds = timeout_seconds
        self._request = request

    def get_params_class(self) -> Type[ToolParams]:
        return McpResourceParams

    def validate_params(self, toolParams: ToolParams) -> Optional[str]:
        assert isinstance(toolParams, McpResourceParams)
        if toolParams.action == "read" and not toolParams.uri:
            return "read needs a uri"
        if toolParams.action != "read" and toolParams.uri:
            return f"{toolParams.action} takes no uri; use read"
        if toolParams.action == "read" and toolParams.cursor:
            return "read takes no cursor"
        return None

    def approval_scope(self, params: Dict[str, Any]) -> ApprovalScope:
        # An untrusted server's reads are approved one by one, like its tools.
        return ApprovalScope(saveable=False)

    async def run(self, toolParams: ToolParams) -> ToolResult:
        from kerkov.core.mcp.connection import McpCallError

        assert isinstance(toolParams, McpResourceParams)
        action = toolParams.action
        argument = toolParams.uri if action == "read" else toolParams.cursor
        try:
            result = await self._request(
                self.server_key, action, argument, self.timeout_seconds
            )
        except McpCallError as exc:
            return ToolResult(success=False, output="", error=str(exc))
        if action == "read":
            return adapt_read_result(
                result,
                server_key=self.server_key,
                uri=toolParams.uri or "",
                call_id=self.execution_call_id,
            )
        return adapt_resource_listing(
            result, server_key=self.server_key, templates=action == "templates"
        )


class TrustedMcpResourceTool(McpResourceTool):
    """Resources of a server the operator marked `trusted` in `mcp.json`."""

    prompts_every_call = False

    def approval_scope(self, params: Dict[str, Any]) -> ApprovalScope:
        return ApprovalScope.whole_tool(volatile=("cursor",))
