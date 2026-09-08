"""
Base tool interface for Kerkov tools.
"""

from abc import ABC, abstractmethod
from contextvars import ContextVar
from typing import Any, Dict, Optional, Type

from pydantic import BaseModel, ValidationError

from kerkov.core.approval_scope import ApprovalScope
from kerkov.core.logging.logger_factory import get_logger


class ToolParams(BaseModel):
    """Interface that provides a common parent for all tool execution params"""

    """All properties MUST use pydantic Fields"""
    pass


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
    """Abstract base class for all Kerkov tools."""

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
        self._execution_call_id: ContextVar[Optional[str]] = ContextVar(
            f"{name}_execution_call_id",
            default=None,
        )

    @property
    def execution_call_id(self) -> Optional[str]:
        """Current LLM tool-call id while `execute()` is running, if any."""
        return self._execution_call_id.get()

    @abstractmethod
    def get_params_class(self) -> Type[ToolParams]:
        """Returns the type of a given tool's ToolParams implementation."""
        pass

    async def execute(
        self, dict_params: Dict[str, Any], *, call_id: Optional[str] = None
    ) -> ToolResult:
        """
        Wrapper function that validates the given tool params and then executes.

        Args:
            dict_params: Tool-specific parameters.
            call_id: Optional provider tool-call id for this execution.

        Returns:
            ToolResult with execution output
        """
        call_id_token = self._execution_call_id.set(call_id)
        try:
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
                self.logger.info(
                    f"execute() succeeded, output_length={len(result.output)}"
                )
            else:
                self.logger.error(f"execute() failed: {result.error}")
            return result
        finally:
            self._execution_call_id.reset(call_id_token)

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
    def approval_scope(self, params: Dict[str, Any]) -> ApprovalScope:
        """Describe how approving one call generalizes into a saved allow rule.

        The approval prompt offers the returned breadth as a single keystroke,
        so a tool may only widen to a boundary its own arguments define — an
        origin, a directory, an action. `ApprovalScope.exact_only()` is the
        answer whenever they define none; it is abstract rather than defaulted
        because a tool inheriting someone else's idea of "broad enough" is how
        a grant nobody chose gets made.
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
            name=self.name,
            description=self.description,
            parameters=params_class.model_json_schema(),
        )
