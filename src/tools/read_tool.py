from pathlib import Path
from typing import Optional
from pydantic import Field

from src.tools.base_tool import BaseTool, ToolApprovalLevel, ToolParams, ToolResult

class ReadToolParams(ToolParams):
    file_path: str = Field(description="Path to the file to read")
    max_size: Optional[int] = Field(default=None, description="Maximum file size to read in bytes (optional, default: 10MB)")

class ReadTool(BaseTool):
    """Read content from files."""

    # Max file size to read (10 MB)
    MAX_FILE_SIZE = 10 * 1024 * 1024

    def __init__(self):
        super().__init__(
            name="read",
            description="Read content from a file. Returns the file contents as text. Has a maximum file size limit of 10MB for safety.",
        )

    def get_approval_level(self):
        return ToolApprovalLevel.NONE

    def get_params_class(self):
        return ReadToolParams

    def validate_params(self, toolParams: ReadToolParams) -> bool:
        """
        Validate read tool parameters.

        Args:
            **kwargs: Must contain 'file_path' parameter

        Returns:
            True if parameters are valid
        """
        if not toolParams.file_path.strip():
            return "File path must not be empty"
        
        if toolParams.max_size and toolParams.max_size > 0:
            return "Max file size must be greater than 0"
        
        return None

    async def run(self, toolParams: ReadToolParams) -> ToolResult:
        try:
            path = Path(toolParams.file_path).resolve()

            if not path.exists():
                return ToolResult(
                    success=False,
                    output="",
                    error=f"File not found: {toolParams.file_path}",
                    metadata={"file_path": str(path)},
                )

            if not path.is_file():
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Not a file: {toolParams.file_path}",
                    metadata={"file_path": str(path)},
                )

            file_size = path.stat().st_size

            size_limit = toolParams.max_size if toolParams.max_size is not None else self.MAX_FILE_SIZE
            if file_size > size_limit:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"File too large: {file_size} bytes (max: {size_limit} bytes)",
                    metadata={"file_path": str(path), "file_size": file_size},
                )

            with open(path, "r", encoding="utf-8") as f:
                content = f.read()

            return ToolResult(
                success=True,
                output=content,
                error=None,
                metadata={
                    "file_path": str(path),
                    "file_size": file_size,
                    "encoding": "utf-8",
                },
            )

        except UnicodeDecodeError:
            try:
                path = Path(toolParams.file_path).resolve()
                with open(path, "rb") as f:
                    content = f.read()
                return ToolResult(
                    success=True,
                    output=f"[Binary file - {len(content)} bytes]\n{content[:500].hex()}...",
                    error=None,
                    metadata={
                        "file_path": str(path),
                        "file_size": len(content),
                        "encoding": "binary",
                    },
                )
            except Exception as e:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Error reading binary file: {str(e)}",
                    metadata={"file_path": toolParams.file_path},
                )
        except PermissionError:
            return ToolResult(
                success=False,
                output="",
                error=f"Permission denied: Cannot read {toolParams.file_path}",
                metadata={"file_path": toolParams.file_path},
            )
        except OSError as e:
            return ToolResult(
                success=False,
                output="",
                error=f"OS error: {str(e)}",
                metadata={"file_path": toolParams.file_path},
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Read error: {str(e)}",
                metadata={"file_path": toolParams.file_path},
            )
        