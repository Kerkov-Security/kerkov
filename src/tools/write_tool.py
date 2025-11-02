"""
File writing tool with security controls.
"""

import os
from pathlib import Path
from typing import Optional

from src.core.config_manager import get_config
from src.tools.base_tool import BaseTool, ToolResult


class WriteTool(BaseTool):
    """Write content to files with security controls."""

    def __init__(self):
        """Initialize the write tool."""
        super().__init__(
            name="write",
            description="Write content to a file. Can create new files or overwrite existing ones. Use 'append' parameter to append to existing files instead of overwriting.",
        )
        self.config = get_config()

    def validate_params(self, **kwargs) -> bool:
        """
        Validate write tool parameters.

        Args:
            **kwargs: Must contain 'file_path' and 'content' parameters

        Returns:
            True if parameters are valid
        """
        if "file_path" not in kwargs or "content" not in kwargs:
            return False
        if not isinstance(kwargs["file_path"], str):
            return False
        if not isinstance(kwargs["content"], str):
            return False
        if not kwargs["file_path"].strip():
            return False
        return True

    async def execute(
        self,
        file_path: str,
        content: str,
        append: bool = False,
        **kwargs
    ) -> ToolResult:
        """
        Write content to a file.

        Args:
            file_path: Path to the file to write
            content: Content to write to the file
            append: If True, append to file instead of overwriting (default: False)
            **kwargs: Additional parameters

        Returns:
            ToolResult with operation status
        """
        if not self.validate_params(file_path=file_path, content=content):
            return ToolResult(
                success=False,
                output="",
                error="Invalid parameters: file_path and content are required",
            )

        if self.config.sandbox_mode:
            return ToolResult(
                success=False,
                output="",
                error="Sandbox mode is enabled. File writing is disabled. Set SANDBOX_MODE=false in .env to enable.",
            )

        try:
            # Resolve the file path
            path = Path(file_path).resolve()

            # Create parent directories if they don't exist
            path.parent.mkdir(parents=True, exist_ok=True)

            # Write or append to the file
            mode = "a" if append else "w"
            with open(path, mode, encoding="utf-8") as f:
                f.write(content)

            # Get file size for metadata
            file_size = path.stat().st_size

            action = "appended to" if append else "written to"
            return ToolResult(
                success=True,
                output=f"Successfully {action} {file_path} ({file_size} bytes)",
                error=None,
                metadata={
                    "file_path": str(path),
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
