"""
Base tool interface for Kerkov Bot tools.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from pydantic import BaseModel

class ToolResult(BaseModel):
    """Standard result format for tool execution."""

    success: bool
    output: str
    error: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

class BaseTool(ABC):
    """Abstract base class for all Kerkov Bot tools."""

    def __init__(self, name: str, description: str):
        """
        Initialize the tool.

        Args:
            name: Tool name (e.g., "bash", "nmap", "shodan")
            description: Human-readable description of what the tool does
        """
        self.name = name
        self.description = description

    @abstractmethod
    async def execute(self, **kwargs) -> ToolResult:
        """
        Execute the tool with given parameters.

        Args:
            **kwargs: Tool-specific parameters

        Returns:
            ToolResult with execution output
        """
        pass

    @abstractmethod
    def validate_params(self, **kwargs) -> bool:
        """
        Validate parameters before execution.

        Args:
            **kwargs: Parameters to validate

        Returns:
            True if parameters are valid, False otherwise
        """
        pass

    def get_info(self) -> Dict[str, str]:
        """
        Get tool information.

        Returns:
            Dictionary with tool name and description
        """
        return {
            "name": self.name,
            "description": self.description,
        }
