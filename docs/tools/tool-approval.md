# Tool Approval System

The tool approval system provides user oversight over tool execution. It allows users to approve, deny, or auto-approve tool calls based on their sensitivity level.

## Approval Levels

Each tool declares an approval level via `get_approval_level()`:

### `NONE`

No approval required. The tool executes immediately without user intervention.

**Use for:** Read-only, non-destructive operations that pose no security risk.

**Examples:**
- `read` - Reading file contents
- `persistent_shell_info` - Getting shell session metadata
- `persistent_shell_get_history` - Viewing command history

### `OPTIONAL`

User is prompted for approval, with the option to auto-approve for the remainder of the session.

**Use for:** Operations that modify state or interact with external systems, but are common enough that repeated prompts would be disruptive.

**Examples:**
- `shell` - Executing shell commands
- `write` - Writing to files
- `fetch` - Making HTTP requests
- `persistent_shell_execute_cmd` - Running commands in persistent shell
- `persistent_shell_send_command` - Sending commands to persistent shell

### `REQUIRED`

User must approve every invocation. No auto-approve option is available.

**Use for:** Sensitive operations where each invocation should be explicitly authorized.

**Examples:**
- `crypto_lab` - Cryptographic operations (encryption, key generation, etc.)
- `persistent_shell_start` - Starting a new shell session
- `persistent_shell_terminate` - Terminating a shell session

## Current Tool Assignments

| Approval Level | Tools |
|----------------|-------|
| **REQUIRED** | `crypto_lab`, `persistent_shell_start`, `persistent_shell_terminate` |
| **OPTIONAL** | `shell`, `write`, `fetch`, `persistent_shell_execute_cmd`, `persistent_shell_send_command` |
| **NONE** | `read`, `persistent_shell_get_history`, `persistent_shell_info` |

## Implementation

### ToolApprovalCliHelper

The approval flow is managed by `ToolApprovalCliHelper` (`src/cli/tool_approval_helper.py`):

```python
class ToolApprovalCliHelper:
    def __init__(self):
        self.optional_tool_approvals: set[str] = set()

    def clear_optional_approvals(self):
        self.optional_tool_approvals.clear()

    def get_tool_usage_approval(self, tool: BaseTool, parameters: Dict[str, Any]) -> ToolApprovalResult:
        approval_level = tool.get_approval_level()

        if approval_level == ToolApprovalLevel.NONE:
            return ToolApprovalResult(approved=True)
        elif approval_level == ToolApprovalLevel.OPTIONAL:
            if tool.name in self.optional_tool_approvals:
                return ToolApprovalResult(approved=True)
            return self._prompt_approval_box(tool, parameters, is_optional=True)
        elif approval_level == ToolApprovalLevel.REQUIRED:
            return self._prompt_approval_box(tool, parameters, is_optional=False)
```

### Approval Flow

1. **NONE**: Returns `approved=True` immediately
2. **OPTIONAL**:
   - If tool was previously auto-approved this session, returns `approved=True`
   - Otherwise, prompts user with options: Approve, Deny, or Approve for session
3. **REQUIRED**: Always prompts user with options: Approve or Deny

### User Prompt

When approval is required, users see a panel displaying:
- Tool name
- Parameters being passed
- Available choices:
  - `[1]` Approve - Execute this tool call
  - `[2]` Deny - Reject this tool call (with optional reason)
  - `[3]` Approve for session - (OPTIONAL only) Auto-approve this tool for the rest of the session

### ToolApprovalResult

```python
class ToolApprovalResult(BaseModel):
    approved: bool
    user_message: Optional[str] = None  # Populated when user denies with a reason
```

## Guidelines for Choosing Approval Levels

When implementing a new tool, consider:

| Question | If Yes | If No |
|----------|--------|-------|
| Does it only read data without side effects? | Consider NONE | - |
| Does it modify files, system state, or make network requests? | OPTIONAL or REQUIRED | Consider NONE |
| Is it a common operation that users will invoke frequently? | OPTIONAL | - |
| Does it involve sensitive operations (crypto, credentials, sessions)? | REQUIRED | - |
| Could misuse cause significant harm? | REQUIRED | OPTIONAL |

### Examples of Reasoning

- **`read` = NONE**: Only reads files, no side effects
- **`write` = OPTIONAL**: Modifies filesystem, but common operation
- **`shell` = OPTIONAL**: Executes commands (risky), but frequent use makes REQUIRED too disruptive
- **`crypto_lab` = REQUIRED**: Cryptographic operations are sensitive and should be explicitly approved
- **`persistent_shell_start` = REQUIRED**: Starting a shell session is a significant action that establishes a persistent process
