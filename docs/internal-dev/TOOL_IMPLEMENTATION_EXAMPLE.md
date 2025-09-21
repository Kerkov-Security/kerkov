# Tool Implementation Example - BashTool

## How It Works

When you want to give the LLM a capability, you:
1. **Define the tool schema** (tells LLM how to use it)
2. **Implement the tool logic** (actual execution in your app)
3. **Handle the tool call** (parse LLM's request and execute)
4. **Return results** (send output back to LLM)

## Complete BashTool Implementation

### 1. Tool Definition (What the LLM Sees)

```python
# This is sent to the LLM API as part of the tools parameter
bash_tool_definition = {
    "name": "execute_bash",
    "description": "Execute bash commands in a Linux environment. Use for file operations, system information, running scripts, etc.",
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The bash command to execute"
            },
            "working_directory": {
                "type": "string",
                "description": "Directory to execute command in (optional)",
                "optional": True
            },
            "timeout": {
                "type": "integer",
                "description": "Maximum execution time in seconds (default: 30)",
                "default": 30
            },
            "run_as_root": {
                "type": "boolean",
                "description": "Whether to run with sudo (requires permission)",
                "default": False
            }
        },
        "required": ["command"]
    }
}
```

### 2. Tool Implementation (Your Application Code)

```python
import subprocess
import asyncio
import os
import shlex
from pathlib import Path
from typing import Dict, Any, Optional
import resource

class BashTool:
    def __init__(self, config: Dict[str, Any]):
        self.max_timeout = config.get('max_timeout', 60)
        self.allowed_commands = config.get('allowed_commands', None)
        self.blocked_commands = config.get('blocked_commands', [
            'rm -rf /',
            'dd if=/dev/zero of=/dev/sda',
            # Add more dangerous commands
        ])
        self.require_sudo_approval = config.get('require_sudo_approval', True)
        self.sandbox_mode = config.get('sandbox_mode', False)

    async def execute(self,
                     command: str,
                     working_directory: Optional[str] = None,
                     timeout: int = 30,
                     run_as_root: bool = False) -> Dict[str, Any]:
        """
        Execute a bash command with safety checks and constraints.

        Returns:
            Dictionary with stdout, stderr, exit_code, and execution_time
        """

        # Safety validation
        if not self._validate_command(command):
            return {
                "error": "Command blocked by safety policy",
                "stdout": "",
                "stderr": "This command has been blocked for safety reasons",
                "exit_code": -1
            }

        # Handle sudo requests
        if run_as_root:
            if not await self._approve_sudo(command):
                return {
                    "error": "Sudo permission denied",
                    "stdout": "",
                    "stderr": "Root access was not approved for this command",
                    "exit_code": -1
                }
            command = f"sudo {command}"

        # Prepare execution environment
        env = os.environ.copy()
        if self.sandbox_mode:
            env = self._create_sandbox_env(env)

        # Set working directory
        cwd = Path(working_directory) if working_directory else Path.cwd()
        if not cwd.exists():
            return {
                "error": f"Working directory {cwd} does not exist",
                "stdout": "",
                "stderr": f"Directory not found: {cwd}",
                "exit_code": -1
            }

        # Execute command
        try:
            start_time = asyncio.get_event_loop().time()

            # Use shlex to properly parse the command
            if os.name == 'nt':  # Windows
                # Windows doesn't use the same shell syntax
                process = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(cwd),
                    env=env
                )
            else:  # Unix/Linux/Mac
                process = await asyncio.create_subprocess_exec(
                    '/bin/bash', '-c', command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(cwd),
                    env=env,
                    preexec_fn=lambda: self._set_resource_limits(timeout) if not os.name == 'nt' else None
                )

            # Wait for completion with timeout
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=min(timeout, self.max_timeout)
                )
                execution_time = asyncio.get_event_loop().time() - start_time

                return {
                    "stdout": stdout.decode('utf-8', errors='replace'),
                    "stderr": stderr.decode('utf-8', errors='replace'),
                    "exit_code": process.returncode,
                    "execution_time": execution_time,
                    "truncated": self._check_if_truncated(stdout, stderr)
                }

            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return {
                    "error": f"Command timed out after {timeout} seconds",
                    "stdout": "",
                    "stderr": f"Execution exceeded {timeout} second timeout",
                    "exit_code": -1
                }

        except Exception as e:
            return {
                "error": f"Execution failed: {str(e)}",
                "stdout": "",
                "stderr": str(e),
                "exit_code": -1
            }

    def _validate_command(self, command: str) -> bool:
        """Validate command against security policies."""

        # Check blocked commands
        for blocked in self.blocked_commands:
            if blocked in command:
                return False

        # If allowlist is defined, check against it
        if self.allowed_commands:
            base_command = shlex.split(command)[0] if command else ""
            if base_command not in self.allowed_commands:
                return False

        # Check for dangerous patterns
        dangerous_patterns = [
            '`',  # Command substitution
            '$(',  # Command substitution
            '&&',  # Command chaining (might want to allow)
            '||',  # Command chaining (might want to allow)
            ';',   # Command separator (might want to allow)
            '>',   # Redirection (might want to allow)
            '|',   # Pipe (might want to allow)
        ]

        # You might want to be more selective about what to block
        # This is just an example

        return True

    async def _approve_sudo(self, command: str) -> bool:
        """Get user approval for sudo commands."""
        if not self.require_sudo_approval:
            return True

        # In a real implementation, this would prompt the user
        print(f"\n⚠️  SUDO REQUEST: {command}")
        approval = input("Approve? (y/n): ")
        return approval.lower() == 'y'

    def _set_resource_limits(self, timeout: int):
        """Set resource limits for the process (Unix only)."""
        # CPU time limit
        resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout))

        # Memory limit (1GB)
        resource.setrlimit(resource.RLIMIT_AS, (1024 * 1024 * 1024, 1024 * 1024 * 1024))

        # Prevent fork bombs
        resource.setrlimit(resource.RLIMIT_NPROC, (100, 100))

    def _create_sandbox_env(self, env: Dict) -> Dict:
        """Create sandboxed environment variables."""
        # Remove sensitive variables
        sandbox_env = env.copy()
        sensitive_vars = ['AWS_SECRET_ACCESS_KEY', 'API_KEY', 'TOKEN', 'PASSWORD']

        for var in list(sandbox_env.keys()):
            for sensitive in sensitive_vars:
                if sensitive in var.upper():
                    del sandbox_env[var]

        return sandbox_env

    def _check_if_truncated(self, stdout: bytes, stderr: bytes) -> bool:
        """Check if output was truncated."""
        max_output_size = 1024 * 1024  # 1MB
        return len(stdout) + len(stderr) > max_output_size
```

### 3. Integration with LLM

```python
class ToolRegistry:
    def __init__(self):
        self.tools = {}
        self.tool_definitions = []

        # Register BashTool
        bash_tool = BashTool(config={
            'max_timeout': 60,
            'sandbox_mode': True,
            'require_sudo_approval': True
        })

        self.register_tool('execute_bash', bash_tool, bash_tool_definition)

    def register_tool(self, name: str, implementation: Any, definition: Dict):
        """Register a tool with its implementation and definition."""
        self.tools[name] = implementation
        self.tool_definitions.append(definition)

    async def execute_tool_call(self, tool_call: Dict) -> Dict:
        """Execute a tool call from the LLM."""
        tool_name = tool_call['function']['name']
        arguments = json.loads(tool_call['function']['arguments'])

        if tool_name not in self.tools:
            return {
                "error": f"Unknown tool: {tool_name}",
                "tool_call_id": tool_call['id']
            }

        tool = self.tools[tool_name]

        # Log the tool execution for audit
        self.log_tool_execution(tool_name, arguments)

        # Execute the tool
        result = await tool.execute(**arguments)

        return {
            "tool_call_id": tool_call['id'],
            "result": result
        }

    def log_tool_execution(self, tool_name: str, arguments: Dict):
        """Log tool executions for security audit."""
        with open('tool_executions.log', 'a') as f:
            f.write(json.dumps({
                'timestamp': datetime.now().isoformat(),
                'tool': tool_name,
                'arguments': arguments
            }) + '\n')
```

### 4. Complete Flow Example

```python
# User asks: "Check what Python version is installed and list Python files"

# 1. LLM receives the request and decides to use the bash tool
llm_response = {
    "content": "I'll check the Python version and list Python files in the current directory.",
    "tool_calls": [
        {
            "id": "call_001",
            "type": "function",
            "function": {
                "name": "execute_bash",
                "arguments": json.dumps({
                    "command": "python --version"
                })
            }
        }
    ]
}

# 2. Your app executes the tool
tool_result = await tool_registry.execute_tool_call(llm_response['tool_calls'][0])
# Result: {"stdout": "Python 3.10.6", "stderr": "", "exit_code": 0}

# 3. Send result back to LLM
conversation.add_message('tool', json.dumps(tool_result))

# 4. LLM continues with next command
llm_response_2 = {
    "content": "Python 3.10.6 is installed. Now let me list the Python files.",
    "tool_calls": [
        {
            "id": "call_002",
            "type": "function",
            "function": {
                "name": "execute_bash",
                "arguments": json.dumps({
                    "command": "find . -name '*.py' -type f | head -20"
                })
            }
        }
    ]
}

# 5. Execute and continue the conversation...
```

## Security Considerations for BashTool

1. **Command Validation**: Always validate against blocked/allowed lists
2. **Resource Limits**: Prevent infinite loops and resource exhaustion
3. **Sandboxing**: Run in isolated environment when possible
4. **Audit Logging**: Log all executions for security review
5. **Output Limits**: Truncate large outputs to prevent memory issues
6. **Timeout Enforcement**: Kill long-running processes
7. **Permission Control**: Require explicit approval for sudo
8. **Environment Sanitization**: Remove sensitive variables

## Other Tool Examples

```python
# File Read Tool
file_read_tool = {
    "name": "read_file",
    "description": "Read contents of a file",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path to read"},
            "start_line": {"type": "integer", "optional": True},
            "end_line": {"type": "integer", "optional": True}
        },
        "required": ["path"]
    }
}

# Security Scan Tool
security_scan_tool = {
    "name": "nmap_scan",
    "description": "Run an nmap scan on a target",
    "parameters": {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "IP or hostname"},
            "scan_type": {
                "type": "string",
                "enum": ["quick", "full", "stealth", "udp"],
                "description": "Type of scan to perform"
            },
            "ports": {"type": "string", "optional": True}
        },
        "required": ["target", "scan_type"]
    }
}
```

The key is that the LLM never directly executes anything - it just requests tools with specific parameters, and your application handles the actual execution with all necessary safety checks.