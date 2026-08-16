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

All tools inherit from `BaseTool` (defined in `src/kerkov/tools/base_tool.py`):

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

Tools are registered through `ToolSuite` (`src/kerkov/tools/tool_suite.py`):

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
| `browser` | Playwright browser session: navigation, snapshots, clicks, typing, storage, screenshots, JS evaluation |
| `process` | Drive and inspect backgrounded `exec` sessions (poll, log, write, send_keys, kill, etc.) |
| `container_lifecycle` | Start, monitor, inspect, or stop Docker Compose projects while storing noisy output in workspace artifacts |
| `zap` | Crawl authorized web targets, run ZAP active scans, monitor progress, stop scans, and retrieve alerts |
| `skill` | Look up and invoke SKILL.md skills |
| `delegate` | Spawn isolated sub-agents for parallel or batched sub-tasks |

Approval behaviour for each tool is configured through rules in `config/approvals.yaml` and `~/.kerkov/approvals.yaml` rather than being hardcoded per-tool. See [Tool Approval](../tool-approval.md) for details.

## OWASP ZAP Tool

No setup command is required. The first approved spider or active scan
generates a random API key, starts the official stable ZAP Docker image, binds
its published API port to `127.0.0.1`, waits for readiness, then stores the key
through the credential manager and the service URL in
`~/.kerkov/settings.json`. The container carries an
`org.kerkov.managed=zap` label; `/zap stop` refuses to remove an unlabeled
container that happens to use the same name.

Kerkov streams the generated key over standard input into a mode-`0600`
configuration file inside the container; it is never included in Docker argv
or environment metadata. The published listener enables only ZAP's
authenticated API. The unauthenticated forward proxy stays on container
loopback and is not published. The managed container is attached to its own
labeled bridge network with inter-container communication disabled. Kerkov
does not use Docker's automatic `--rm` cleanup, allowing it to capture a failed
startup's labeled container logs before explicitly cleaning up.

The Docker daemon must already be running when the first scan starts. Automatic
startup reports whether the Docker CLI is missing, the daemon is unreachable,
or the current user lacks daemon access before generating or saving
configuration.

Managed containers receive a `host.docker.internal` host-gateway mapping.
Kerkov translates loopback scan targets to that alias while preserving their
port, path, and query, so ZAP does not accidentally scan its own container.
Native Linux target services must listen on an interface reachable from the
Docker bridge.

The `zap` tool exposes these actions:

| Action | Purpose | Default approval |
|---|---|---|
| `status` | Validate the API and return the ZAP version | Allowed |
| `spider_start` | Crawl a target and feed responses through passive scanning | Prompted |
| `spider_status` | Return spider progress, then wait for queued passive scanning | Allowed |
| `active_scan_start` | Launch intrusive vulnerability tests against known resources | Prompted |
| `active_scan_status` | Return active-scan completion percentage | Allowed |
| `alerts` | Retrieve normalized, bounded findings | Allowed |
| `scan_stop` | Stop a spider or active scan by id | Prompted |

Spider the authorized target first, wait for it to complete, then run the
active scan. When crawling reaches 100% but passive records remain queued,
`spider_status` reports `passive_scanning` at 99% and reports completion only
after the queue drains. The generated API key is sent in the `X-ZAP-API-Key`
header and is never included in tool parameters, API URLs, normal logs, or user
settings. Individual alert fields and the full serialized alert response are
bounded. If the response reaches the total budget, `truncated` is true and
`next_start` supplies the offset for the next request.

## Implementing a New Tool

### 1. Create the Tool Class

Create a new file in `src/kerkov/tools/` (or a subdirectory for related tools):

```python
from typing import Optional
from pydantic import Field

from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult

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

Add the tool to `ToolSuite.INDEPENDENT_TOOLS` in `src/kerkov/tools/tool_suite.py`:

```python
from kerkov.tools.my_tool import MyTool

INDEPENDENT_TOOLS: list[BaseTool] = [
    # ... existing tools ...
    MyTool(),
]
```

Or create a tool suite for related tools (see `PersistentShellToolSuite` for an example).

### 3. Approval behaviour

Tool approval is governed by rules in `config/approvals.yaml` and `~/.kerkov/approvals.yaml`, not by a per-tool property. See [Tool Approval](../tool-approval.md) for the rules syntax and how to configure which calls auto-approve, auto-deny, or prompt the user.

## Browser Tool Runtime

The `browser` tool depends on Playwright. After syncing the uv environment,
install Chromium once with:

```bash
uv run python -m playwright install chromium
```

Snapshots return a compact, agent-friendly accessibility-style view with refs
such as `[ref=e1]`. Click, type, and screenshot actions accept those refs, the
legacy refs such as `button:1`, or plain CSS selectors. The `start` action can
also take a `session_id` to create a predictable named session.

The `evaluate` action runs arbitrary JavaScript in the current page context.
Treat it as a high-risk action when using permissive or autonomous approval
modes. Screenshots are constrained to the configured browser screenshot
directory, and can target a specific ref or selector. Source fetch output is
capped before it is returned to the model.
