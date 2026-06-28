# Tools Overview

Tools are the primary mechanism through which the LLM interacts with the external environment. Each tool represents a discrete capability that the bot can invoke to perform actions like executing commands, reading files, or making network requests.

## Purpose

Tools extend the LLM's capabilities beyond text generation by allowing it to:

- Execute shell commands
- Read and write files
- Fetch content from URLs
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
    ]

    @staticmethod
    def get_tools(shell_manager: ShellManager) -> list[BaseTool]:
        return ToolSuite.INDEPENDENT_TOOLS + PersistentShellToolSuite.get_tools(shell_manager)
```

Related tools can be grouped into a "tool suite" (e.g., `PersistentShellToolSuite`) for better organization.

## Available Tools

| Tool | Description |
|------|-------------|
| `exec` | Execute shell commands (one-shot, interactive session, or daemon) |
| `read` | Read file contents |
| `write` | Write content to files |
| `http` | HTTP requests with optional named credential profiles |
| `browser` | Playwright CLI browser session: navigation, snapshots, clicks, typing, storage, screenshots, JS evaluation |
| `process` | Drive and inspect backgrounded `exec` sessions (poll, log, write, send_keys, kill, etc.) |
| `skill` | Look up and invoke SKILL.md skills |
| `delegate` | Spawn isolated sub-agents for parallel or batched sub-tasks |

Approval behaviour for each tool is configured through rules in `config/approvals.yaml` and `~/.kerkov/approvals.yaml` rather than being hardcoded per-tool. See [Tool Approval](../tool-approval.md) for details.

## Implementing a New Tool

### 1. Create the Tool Class

Create a new file in `src/tools/` (or a subdirectory for related tools):

```python
from typing import Optional
from pydantic import Field

from src.tools.base_tool import BaseTool, ToolParams, ToolResult

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

### 3. Approval behaviour

Tool approval is governed by rules in `config/approvals.yaml` and `~/.kerkov/approvals.yaml`, not by a per-tool property. See [Tool Approval](../tool-approval.md) for the rules syntax and how to configure which calls auto-approve, auto-deny, or prompt the user.

## Browser Tool Runtime

The `browser` tool is backed by Playwright CLI (`@playwright/cli`), not the
Python Playwright package. Kerkov resolves the CLI in this order:

1. `KERKOV_PLAYWRIGHT_CLI`, split like a shell command.
2. `node_modules/.bin/playwright-cli`.
3. A global `playwright-cli` on `PATH`.
4. `npx -y @playwright/cli@latest`.

For a stable local install, use:

```bash
npm install -g @playwright/cli@latest
playwright-cli install-browser
```

The `evaluate` action runs arbitrary JavaScript in the current page context.
Treat it as a high-risk action when using permissive or autonomous approval
modes. Screenshots are constrained to the configured browser screenshot
directory. Playwright CLI working files are written under the active run
workspace at `browser/playwright-cli`, or under `data/browser/playwright-cli`
when no run workspace is active. Source fetch output is capped before it is
returned to the model.
