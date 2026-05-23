from pathlib import Path
from typing import Optional
from pydantic import Field

from src.core.config_manager import get_config
from src.core.run_workspace import RunWorkspaceManager, get_current_workspace
from src.tools.base_tool import BaseTool, ToolApprovalLevel, ToolParams, ToolResult

class WriteToolParams(ToolParams):
    file_path: str = Field(description="Path to the file to write")
    content: str = Field(description="Content to write to the file")
    append: Optional[bool] = Field(default=None, description="If true, append to file instead of overwriting (optional, default false)")

class WriteTool(BaseTool):
    """Write content to files with."""

    def __init__(self, workspace_manager: Optional[RunWorkspaceManager] = None):
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
        self.config = get_config()
        self.workspace_manager = workspace_manager

    def get_approval_level(self):
        return ToolApprovalLevel.OPTIONAL

    def get_approval_level_for_params(self, params: dict) -> ToolApprovalLevel:
        file_path = params.get("file_path")
        if not isinstance(file_path, str):
            return self.get_approval_level()
        if self._is_absolute_path_outside_workspace(file_path):
            return ToolApprovalLevel.REQUIRED
        return self.get_approval_level()

    def get_params_class(self):
        return WriteToolParams

    def validate_params(self, toolParams: WriteToolParams) -> Optional[str]:
        if not toolParams.file_path.strip():
            return "File path must not be empty"
        
        if not toolParams.content.strip():
            return "Content to write must not be empty"
        
        return None

    async def run(self, toolParams: WriteToolParams) -> ToolResult:
        if self.config.sandbox_mode:
            return ToolResult(
                success=False,
                output="",
                error="Sandbox mode is enabled. File writing is disabled. Set SANDBOX_MODE=false in .env to enable.",
            )

        try:
            file_path = toolParams.file_path
            path = self._resolve_write_path(file_path)

            path.parent.mkdir(parents=True, exist_ok=True)

            mode = "a" if toolParams.append else "w"
            with open(path, mode, encoding="utf-8") as f:
                f.write(toolParams.content)

            file_size = path.stat().st_size

            action = "appended to" if toolParams.append else "written to"
            return ToolResult(
                success=True,
                output=f"Successfully {action} {file_path} ({file_size} bytes)",
                error=None,
                metadata={
                    "file_path": str(path),
                    "workspace_path": str(get_current_workspace(self.workspace_manager))
                    if get_current_workspace(self.workspace_manager)
                    else None,
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
        requested = Path(file_path).expanduser()
        if requested.is_absolute():
            return requested.resolve()

        workspace = get_current_workspace(self.workspace_manager)
        if workspace is None:
            return requested.resolve()

        root = workspace.resolve()
        path = (root / requested).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            raise ValueError(f"Relative file path must stay under workspace: {root}")
        return path

    def _is_absolute_path_outside_workspace(self, file_path: str) -> bool:
        requested = Path(file_path).expanduser()
        if not requested.is_absolute():
            return False

        workspace = get_current_workspace(self.workspace_manager)
        if workspace is None:
            return False

        try:
            requested.resolve().relative_to(workspace.resolve())
        except ValueError:
            return True
        return False
