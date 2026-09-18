from pathlib import Path
from typing import Any, Dict, Optional

from pydantic import Field

from kerkov.core.approval_scope import ApprovalScope
from kerkov.core.run_workspace import get_current_workspace, resolve_file_path
from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult


class WriteToolParams(ToolParams):
    file_path: str = Field(description="Path to the file to write")
    content: str = Field(description="Content to write to the file")
    append: Optional[bool] = Field(
        default=None,
        description="If true, append to file instead of overwriting (optional, default false)",
    )


class WriteTool(BaseTool):
    """Write content to files with."""

    def __init__(self, *, write_tool_enabled: bool = False) -> None:
        super().__init__(
            name="write",
            description=(
                "Write content to a file. Relative paths are written under the "
                "active engagement workspace by default; absolute paths are "
                "honored as explicit user paths. Can create new files or "
                "overwrite existing ones. Use 'append' parameter to append to "
                "existing files instead of overwriting."
            ),
        )
        self.write_tool_enabled = write_tool_enabled

    def get_params_class(self):
        return WriteToolParams

    def approval_scope(self, params: Dict[str, Any]) -> ApprovalScope:
        """Widen to the directory holding the file; never carry the payload.

        `content` is volatile because a rule pinned to one body would never
        match again, and because writing it out would copy whatever the file
        held — a key, a token — into the rules file in cleartext.
        """
        return ApprovalScope.parent_directory(
            params, "file_path", volatile=("content",)
        )

    def validate_params(self, toolParams: WriteToolParams) -> Optional[str]:
        if not toolParams.file_path.strip():
            return "File path must not be empty"

        if not toolParams.content.strip():
            return "Content to write must not be empty"

        return None

    async def run(self, toolParams: WriteToolParams) -> ToolResult:
        if not self.write_tool_enabled:
            return ToolResult(
                success=False,
                output="",
                error=(
                    "The write tool is disabled. Set WRITE_TOOL_ENABLED=true "
                    "to enable the write tool."
                ),
            )

        try:
            file_path = toolParams.file_path
            path = self._resolve_write_path(file_path)

            path.parent.mkdir(parents=True, exist_ok=True)

            mode = "a" if toolParams.append else "w"
            with open(path, mode, encoding="utf-8") as f:
                f.write(toolParams.content)

            file_size = path.stat().st_size

            workspace = get_current_workspace()
            action = "appended to" if toolParams.append else "written to"
            return ToolResult(
                success=True,
                output=f"Successfully {action} {file_path} ({file_size} bytes)",
                error=None,
                metadata={
                    "file_path": str(path),
                    "workspace_path": str(workspace) if workspace else None,
                    "file_size": file_size,
                    "mode": mode,
                },
            )

        except PermissionError:
            return ToolResult(
                success=False,
                output="",
                error=f"Permission denied: Cannot write to {file_path}",
                metadata={"file_path": file_path},
            )
        except OSError as e:
            return ToolResult(
                success=False,
                output="",
                error=f"OS error: {str(e)}",
                metadata={"file_path": file_path},
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Write error: {str(e)}",
                metadata={"file_path": file_path},
            )

    def _resolve_write_path(self, file_path: str) -> Path:
        return resolve_file_path(file_path)

    def _is_absolute_path_outside_workspace(self, file_path: str) -> bool:
        requested = Path(file_path).expanduser()
        if not requested.is_absolute():
            return False

        workspace = get_current_workspace()
        if workspace is None:
            return False

        try:
            requested.resolve().relative_to(workspace.resolve())
        except ValueError:
            return True
        return False
