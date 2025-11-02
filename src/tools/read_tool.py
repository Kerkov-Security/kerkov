"""
File reading tool with safety controls.
"""

from pathlib import Path
from typing import Optional

from src.tools.base_tool import BaseTool, ToolResult


class ReadTool(BaseTool):
    """Read content from files with safety controls."""

    # Max file size to read (10 MB)
    MAX_FILE_SIZE = 10 * 1024 * 1024

    def __init__(self):
        """Initialize the read tool."""
        super().__init__(
            name="read",
            description="Read content from a file. Returns the file contents as text. Has a maximum file size limit of 10MB for safety.",
        )

    def validate_params(self, **kwargs) -> bool:
        """
        Validate read tool parameters.

        Args:
            **kwargs: Must contain 'file_path' parameter

        Returns:
            True if parameters are valid
        """
        if "file_path" not in kwargs:
            return False
        if not isinstance(kwargs["file_path"], str):
            return False
        if not kwargs["file_path"].strip():
            return False
        return True

    async def execute(
        self,
        file_path: str,
        max_size: Optional[int] = None,
        **kwargs
    ) -> ToolResult:
        """
        Read content from a file.

        Args:
            file_path: Path to the file to read
            max_size: Maximum file size to read in bytes (default: 10MB)
            **kwargs: Additional parameters

        Returns:
            ToolResult with file contents
        """
        if not self.validate_params(file_path=file_path):
            return ToolResult(
                success=False,
                output="",
                error="Invalid parameter: file_path is required",
            )

        try:
            # Resolve the file path
            path = Path(file_path).resolve()

            # Check if file exists
            if not path.exists():
                return ToolResult(
                    success=False,
                    output="",
                    error=f"File not found: {file_path}",
                    metadata={"file_path": str(path)},
                )

            # Check if it's a file (not a directory)
            if not path.is_file():
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Not a file: {file_path}",
                    metadata={"file_path": str(path)},
                )

            # Get file size
            file_size = path.stat().st_size

            # Check file size limit
            size_limit = max_size if max_size is not None else self.MAX_FILE_SIZE
            if file_size > size_limit:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"File too large: {file_size} bytes (max: {size_limit} bytes)",
                    metadata={"file_path": str(path), "file_size": file_size},
                )

            # Read the file
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
            # Try reading as binary if UTF-8 fails
            try:
                path = Path(file_path).resolve()
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
                    metadata={"file_path": file_path},
                )
        except PermissionError:
            return ToolResult(
                success=False,
                output="",
                error=f"Permission denied: Cannot read {file_path}",
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
                error=f"Read error: {str(e)}",
                metadata={"file_path": file_path},
            )
