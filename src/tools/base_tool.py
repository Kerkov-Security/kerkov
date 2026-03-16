"""
Base tool interface for Kerkov Bot tools.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Type
from enum import Enum
from pydantic import BaseModel, ValidationError

from src.core.logging.logger_factory import get_logger

class ToolParams(BaseModel):
    """Interface that provides a common parent for all tool execution params"""
    """All properties MUST use pydantic Fields"""
    pass

class ToolApprovalLevel(Enum):
    NONE = "none"
    OPTIONAL = "optional"
    REQUIRED = "required"

class GenericToolSchema(BaseModel):
    name: str
    description: str
    parameters: Dict[str, Any]
    
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
        self.logger = get_logger(f"Tool.{name}")

    @abstractmethod
    def get_approval_level(self) -> ToolApprovalLevel:
        """Returns the type of a given tool's ToolParams implementation."""
        pass

    @abstractmethod
    def get_params_class(self) -> Type[ToolParams]:
        """Returns the type of a given tool's ToolParams implementation."""
        pass

    async def execute(self, dict_params: Dict[str, Any]) -> ToolResult:
        """
        Wrapper function that validates the given tool params and then executes.

        Args:
            **kwargs: Tool-specific parameters

        Returns:
            ToolResult with execution output
        """
        self.logger.info(f"execute() called with params: {dict_params}")
        params_class = self.get_params_class()
        try:
          toolParams = params_class(**dict_params)
        except ValidationError as e:
            self.logger.warning(f"Parameter validation failed: {e}")
            return ToolResult(
                success=False,
                output="",
                error=f"Parameter validation failed: {e}",
            )

        validation_error = self.validate_params(toolParams)
        if validation_error:
            self.logger.warning(f"Invalid parameters: {validation_error}")
            return ToolResult(
                success=False,
                output="",
                error=f"Invalid parameters: {validation_error}",
            )

        result = await self.run(toolParams)
        if result.success:
            self.logger.info(f"execute() succeeded, output_length={len(result.output)}")
        else:
            self.logger.error(f"execute() failed: {result.error}")
        return result

    @abstractmethod
    async def run(self, toolParams: ToolParams) -> ToolResult:
        """
        Run the tool with given parameters.

        Args:
            **kwargs: Tool-specific parameters

        Returns:
            ToolResult with execution output
        """
        pass

    @abstractmethod
    def validate_params(self, toolParams: ToolParams) -> Optional[str]:
        """
        Validate parameters before execution.

        Args:
            toolParams: Parameters to validate

        Returns:
            None if parameters are valid, an error message otherwise
        """
        pass

    def get_generic_schema(self) -> GenericToolSchema:
        params_class = self.get_params_class()
        return GenericToolSchema(
            name = self.name,
            description = self.description,
            parameters = params_class.model_json_schema(),
        )