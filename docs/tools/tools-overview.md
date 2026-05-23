# Tools Overview

Tools are the primary mechanism through which the LLM interacts with the external environment. Each tool represents a discrete capability that the bot can invoke to perform actions like executing commands, reading files, or making network requests.

## Purpose

Tools extend the LLM's capabilities beyond text generation by allowing it to:

- Execute shell commands
- Read and write files
- Fetch content from URLs
- Perform cryptographic operations
- Manage persistent shell sessions

## Architecture

### Base Classes

All tools inherit from `BaseTool` (defined in `src/tools/base_tool.py`):

```python
class BaseTool(ABC):
    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description

    @abstractmethod
    def get_approval_level(self) -> ToolApprovalLevel: ...

    @abstractmethod
    def get_params_class(self) -> Type[ToolParams]: ...

    async def execute(self, dict_params: Dict[str, Any]) -> ToolResult: ...

    @abstractmethod
    async def run(self, toolParams: ToolParams) -> ToolResult: ...

    @abstractmethod
    def validate_params(self, toolParams: ToolParams) -> Optional[str]: ...

    def get_generic_schema(self) -> GenericToolSchema: ...
```

### Parameter Validation

Tools use Pydantic models for parameter validation. Each tool defines a `ToolParams` subclass:

```python
class MyToolParams(ToolParams):
    required_param: str = Field(description="A required parameter")
    optional_param: Optional[int] = Field(default=None, description="An optional parameter")
```

### Execution Flow

1. The LLM requests a tool call with parameters
2. `execute()` validates parameters using the tool's `ToolParams` class
3. `validate_params()` performs any additional validation
4. `run()` executes the tool logic
5. A `ToolResult` is returned to the LLM

## Tool Registration

Tools are registered through `ToolSuite` (`src/tools/tool_suite.py`):

```python
class ToolSuite:
    INDEPENDENT_TOOLS: list[BaseTool] = [
        ShellTool(),
        ReadTool(),
        WriteTool(),
        FetchTool(),
        CryptoLabTool(),
    ]

    @staticmethod
    def get_tools(shell_manager: ShellManager) -> list[BaseTool]:
        return ToolSuite.INDEPENDENT_TOOLS + PersistentShellToolSuite.get_tools(shell_manager)
```

Related tools can be grouped into a "tool suite" (e.g., `PersistentShellToolSuite`) for better organization.

## Available Tools

| Tool | Description | Approval Level |
|------|-------------|----------------|
| `shell` | Execute one-off shell commands | OPTIONAL |
| `read` | Read file contents | NONE |
| `write` | Write content to files | OPTIONAL |
| `fetch` | Fetch content from URLs | OPTIONAL |
| `browser` | Start and interact with a Playwright browser session; supports navigation, snapshots, clicks, typing, storage, sources, network metadata, screenshots, and JavaScript evaluation | OPTIONAL |
| `crypto_lab` | Cryptographic operations (encoding, hashing, encryption) | REQUIRED |
| `persistent_shell_start` | Start a persistent shell session | REQUIRED |
| `persistent_shell_terminate` | Terminate the persistent shell | REQUIRED |
| `persistent_shell_info` | Get shell session info | NONE |
| `persistent_shell_get_history` | Get shell command history | NONE |
| `persistent_shell_execute_cmd` | Execute command and wait for output | OPTIONAL |
| `persistent_shell_send_command` | Send command without waiting | OPTIONAL |

## Implementing a New Tool

### 1. Create the Tool Class

Create a new file in `src/tools/` (or a subdirectory for related tools):

```python
from typing import Optional
from pydantic import Field

from src.tools.base_tool import BaseTool, ToolApprovalLevel, ToolParams, ToolResult

class MyToolParams(ToolParams):
    """Parameters for MyTool."""
    input_value: str = Field(description="The input to process")
    timeout: Optional[float] = Field(default=None, description="Optional timeout in seconds")

class MyTool(BaseTool):
    """Description of what the tool does."""

    def __init__(self):
        super().__init__(
            name="my_tool",
            description="A clear description that helps the LLM understand when to use this tool.",
        )

    def get_approval_level(self) -> ToolApprovalLevel:
        return ToolApprovalLevel.OPTIONAL  # Choose appropriate level

    def get_params_class(self):
        return MyToolParams

    def validate_params(self, toolParams: MyToolParams) -> Optional[str]:
        """Return None if valid, or an error message string."""
        if not toolParams.input_value.strip():
            return "Input value must not be empty"
        return None

    async def run(self, toolParams: MyToolParams) -> ToolResult:
        """Execute the tool logic."""
        try:
            result = do_something(toolParams.input_value)
            return ToolResult(
                success=True,
                output=result,
                metadata={"input": toolParams.input_value},
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Error: {str(e)}",
            )
```

### 2. Register the Tool

Add the tool to `ToolSuite.INDEPENDENT_TOOLS` in `src/tools/tool_suite.py`:

```python
from src.tools.my_tool import MyTool

INDEPENDENT_TOOLS: list[BaseTool] = [
    # ... existing tools ...
    MyTool(),
]
```

Or create a tool suite for related tools (see `PersistentShellToolSuite` for an example).

### 3. Choose the Right Approval Level

See [Tool Approval](./tool-approval.md) for guidance on selecting the appropriate approval level.

## Browser Tool Runtime

The `browser` tool depends on Playwright. After installing Python dependencies,
install Chromium once with:

```bash
python -m playwright install chromium
```

The `evaluate` action runs arbitrary JavaScript in the current page context.
Treat it as a high-risk action when using permissive or autonomous approval
modes. Screenshots are constrained to the configured browser screenshot
directory, and source fetch output is capped before it is returned to the model.
