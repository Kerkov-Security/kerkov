# Autonomy Levels — Development Plan

## Overview

Introduce a tiered autonomy system that controls two linked behaviors:

1. **Explanation verbosity** — how much the LLM narrates its reasoning before tool calls
2. **Approval gating** — whether/when the user must approve tool execution

These are bundled into four levels rather than exposed as independent settings, because
they represent a single trust slider from "walk me through everything" to "just do it."

This also fixes the existing bug where `response.content` (the LLM's explanation text)
is stored but never displayed to the user when tool calls are present.

---

## Level Definitions

| Level | Name | Explanation Behavior | Approval Behavior |
|-------|------|---------------------|-------------------|
| 1 | **Guided** | Detailed explanation before each tool call | All tools require explicit approval (overrides `NONE`/`OPTIONAL`) |
| 2 | **Standard** | Brief explanation before tool batches | Respects per-tool approval levels (`NONE`/`OPTIONAL`/`REQUIRED` as configured) |
| 3 | **Aggressive** | Minimal — high-level intent only for multi-step sequences | Only `REQUIRED`-level tools prompt for approval |
| 4 | **Autonomous** | None — explain after completion if asked | No approval prompts. **Requires explicit opt-in** (env var or startup flag) |

**Default: Level 2 (Standard)**

---

## Level 4 Safety Gate

Level 4 bypasses all tool approval, including `REQUIRED`. For a security tool that
runs shell commands, this is a meaningful safety decision.

**Constraint:** Level 4 can only be activated if `ALLOW_AUTONOMOUS=true` is set in
`.env` (or as an environment variable). The `/autonomy 4` command must check this and
refuse with a warning if the env var is not set. This prevents accidental escalation
via the runtime toggle.

---

## Architecture

### Where autonomy level lives

The autonomy level is **not** part of `ConfigManager` (which is frozen/immutable after
load). Instead, it follows the same pattern as `ModeManager` — a lightweight runtime
manager that reads an initial value from config and supports runtime switching.

```
src/cli/autonomy_manager.py   # New file — owns the current level + switching logic
```

`ConfigManager` gets two new fields for initial/default values only:
- `autonomy_level: int` (1-4, default 2) from `AUTONOMY_LEVEL` env var
- `allow_autonomous: bool` (default False) from `ALLOW_AUTONOMOUS` env var

### Component responsibilities

| Component | Responsibility |
|-----------|---------------|
| `AutonomyManager` | Holds current level, validates transitions, exposes level to other components |
| `ConfigManager` | Provides initial `AUTONOMY_LEVEL` and `ALLOW_AUTONOMOUS` from `.env` |
| `KerkovCLI` | Owns the `AutonomyManager` instance, passes level to prompt builder and approval helper |
| `modes.py` | New prompt fragments keyed by autonomy level (appended to mode prompt) |
| `tool_approval_helper.py` | Checks autonomy level to decide whether to prompt |
| `kerkov_cli.py` | Checks autonomy level to decide whether to display `response.content` before tool execution |
| `input_parser.py` | Routes `/autonomy` command to new message class |

---

## Implementation Steps

### Step 1: Define the AutonomyLevel enum and AutonomyManager

**New file:** `src/cli/autonomy_manager.py`

```python
from enum import IntEnum
from typing import Optional

class AutonomyLevel(IntEnum):
    GUIDED = 1
    STANDARD = 2
    AGGRESSIVE = 3
    AUTONOMOUS = 4
```

`AutonomyManager` class:
- Constructor takes `initial_level: int` and `allow_autonomous: bool`
- Property `current_level -> AutonomyLevel`
- Method `set_level(level: int) -> AutonomyLevel` — validates range, checks
  `allow_autonomous` gate for level 4, raises/returns error if invalid
- Method `list_levels() -> dict[AutonomyLevel, str]` — returns level names/descriptions
  for the `/autonomy` info display
- Static method `parse_level(value: str) -> Optional[AutonomyLevel]` — accepts "1"-"4"
  or names ("guided", "standard", "aggressive", "autonomous")

### Step 2: Add config fields to ConfigManager

**File:** `src/core/config_manager.py`

Add two fields to `ConfigManager`:

```python
autonomy_level: int = Field(2, alias="AUTONOMY_LEVEL")
allow_autonomous: bool = Field(False, alias="ALLOW_AUTONOMOUS")
```

Add `"AUTONOMY_LEVEL"` and `"ALLOW_AUTONOMOUS"` to the env key list in `load_config()`.

Reuse the existing `parse_bool` validator for `allow_autonomous`. Add a validator for
`autonomy_level` that clamps to 1-4.

### Step 3: Wire AutonomyManager into KerkovCLI

**File:** `src/cli/kerkov_cli.py`

In `__init__`:

```python
self.autonomy_manager = AutonomyManager(
    initial_level=self.config.autonomy_level,
    allow_autonomous=self.config.allow_autonomous,
)
```

### Step 4: Add autonomy prompt fragments to modes.py

**File:** `src/cli/modes.py`

Add a dict mapping `AutonomyLevel` to prompt text that gets appended after the
mode-specific overlay. These modify the LLM's communication style, not its domain
behavior:

```python
AUTONOMY_PROMPTS = {
    AutonomyLevel.GUIDED: """
COMMUNICATION STYLE:
- Before EVERY tool call, provide a detailed explanation of:
  - What you are about to do and why
  - What you expect the result to tell you
  - How it fits into your overall approach
- Explain each tool call individually, not as a batch.
- After tool results, briefly interpret what you learned before proceeding.
""",
    AutonomyLevel.STANDARD: """
COMMUNICATION STYLE:
- Before executing tools, provide a brief explanation of your approach and what
  you plan to do.
- When executing multiple tools, summarize the batch rather than explaining each
  individually.
- After completing a sequence of tool calls, summarize findings before moving on.
""",
    AutonomyLevel.AGGRESSIVE: """
COMMUNICATION STYLE:
- Only explain your high-level intent when starting a new phase or multi-step sequence.
- Do not explain individual tool calls.
- Focus output on results and findings, not process.
""",
    AutonomyLevel.AUTONOMOUS: """
COMMUNICATION STYLE:
- Execute tools without explanation. Focus entirely on results.
- Only explain your methodology if the user explicitly asks.
""",
}
```

### Step 5: Inject autonomy prompt into system prompt assembly

**File:** `src/cli/kerkov_cli.py`

In `_apply_system_prompt_if_possible()`, after building `system_prompt` from the mode
(and optional CTF context), append the autonomy prompt fragment:

```python
from src.cli.modes import AUTONOMY_PROMPTS

system_prompt += AUTONOMY_PROMPTS[self.autonomy_manager.current_level]
```

**Important:** When autonomy level changes mid-session via `/autonomy`, clear
conversation history (same as mode switch) so the new prompt takes effect. Document
this in the `/autonomy` command output.

### Step 6: Display `response.content` before tool execution (the core UX fix)

**File:** `src/cli/kerkov_cli.py`

In `get_response()`, inside the `if response.tool_calls:` branch (around line 219),
**before** the "Executing N tool(s)" line, add:

```python
if response.tool_calls:
    # Display LLM explanation text when present — the autonomy prompt
    # controls whether the LLM produces this text, not the display layer.
    if response.content and response.content.strip():
        console.print()
        console.print(Markdown(response.content))
        console.print()

    tool_names = [tc.tool_name for tc in response.tool_calls]
    console.print(f"[dim]-> Executing {len(response.tool_calls)} tool(s): ...")
    # ... rest of existing logic
```

**No display-side gating by autonomy level.** The autonomy prompt fragments (Step 4)
are the sole control over whether the LLM produces explanation text. The display layer
always renders whatever the LLM sends. This keeps one code path and one source of
truth — if the LLM is too verbose at a given level, fix the prompt, not the display.

### Step 7: Modify tool approval logic based on autonomy level

**File:** `src/cli/tool_approval_helper.py`

Change `get_tool_usage_approval()` to accept the current autonomy level and adjust
gating:

```python
async def get_tool_usage_approval(
    self,
    tool: BaseTool,
    parameters: Dict[str, Any],
    autonomy_level: AutonomyLevel,
) -> ToolApprovalResult:
    approval_level = tool.get_approval_level()

    # Level 4 (Autonomous): skip all approval
    if autonomy_level == AutonomyLevel.AUTONOMOUS:
        return ToolApprovalResult(approved=True)

    # Level 3 (Aggressive): only prompt for REQUIRED tools
    if autonomy_level == AutonomyLevel.AGGRESSIVE:
        if approval_level != ToolApprovalLevel.REQUIRED:
            return ToolApprovalResult(approved=True)
        return await self._prompt_approval_box(tool, parameters, is_optional=False)

    # Level 2 (Standard): respect per-tool approval levels (existing behavior)
    if autonomy_level == AutonomyLevel.STANDARD:
        # ... existing NONE/OPTIONAL/REQUIRED logic unchanged
        pass

    # Level 1 (Guided): all tools require approval
    if autonomy_level == AutonomyLevel.GUIDED:
        is_optional = approval_level != ToolApprovalLevel.REQUIRED
        return await self._prompt_approval_box(tool, parameters, is_optional=is_optional)
```

Update the call site in `kerkov_cli.py` to pass the level:

```python
approval_result = await self.tool_approval_helper.get_tool_usage_approval(
    tool, tool_call.parameters, self.autonomy_manager.current_level
)
```

### Step 8: Add `/autonomy` command

**New file:** `src/cli/messages/autonomy_info_message.py`

Displays current autonomy level and lists all levels with descriptions. Pattern
matches `ModeInfoMessage`.

**New file:** `src/cli/messages/switch_autonomy_message.py`

Accepts a level (1-4 or name), calls `autonomy_manager.set_level()`, clears history,
prints confirmation. Pattern matches `SwitchModeMessage`.

**File:** `src/cli/input_parser.py`

Add routing:

```python
elif command == "/autonomy":
    return _determine_autonomy_message(parts)
```

```python
def _determine_autonomy_message(parts: list[str]) -> UserMessage:
    if len(parts) == 1:
        return AutonomyInfoMessage()
    return SwitchAutonomyMessage(parts[1])
```

### Step 9: Update welcome panel

**File:** `src/cli/kerkov_cli.py`

In `_display_welcome()`, add to the commands list:

```
- `/autonomy` to show current autonomy level
- `/autonomy [1-4|guided|standard|aggressive|autonomous]` to change autonomy level
```

Add current autonomy level to the status display:

```
**Autonomy:** Level 2 (Standard)
```

### Step 10: Tests

**New file:** `tests/cli/autonomy_manager_test.py`

Test cases:
- Default level is STANDARD (2)
- Setting levels 1-3 works without `allow_autonomous`
- Setting level 4 fails without `allow_autonomous=True`
- Setting level 4 succeeds with `allow_autonomous=True`
- `parse_level()` handles ints, strings, names, invalid input
- Out-of-range values are rejected

**New file:** `tests/cli/messages/switch_autonomy_message_test.py`

Test cases:
- Valid level switch triggers `autonomy_manager.set_level()` and clears history
- Invalid level prints error

**File:** `tests/cli/tool_approval_helper_test.py` (update existing or create)

Test cases for each autonomy level:
- Level 1: all tools prompt for approval
- Level 2: existing behavior (NONE auto-approved, OPTIONAL cached, REQUIRED always)
- Level 3: only REQUIRED prompts
- Level 4: nothing prompts

**File:** `tests/cli/kerkov_cli_test.py` (update existing or create)

Test case:
- When `response.content` is non-empty and `response.tool_calls` is non-empty,
  content is rendered as Markdown before tool execution output
- When `response.content` is empty/whitespace-only, no extra output is rendered

---

## Files Changed (Summary)

| File | Change |
|------|--------|
| `src/cli/autonomy_manager.py` | **New** — AutonomyLevel enum, AutonomyManager class |
| `src/core/config_manager.py` | Add `autonomy_level`, `allow_autonomous` fields + env keys |
| `src/cli/kerkov_cli.py` | Wire AutonomyManager, display `response.content` before tools, pass level to approval helper, update welcome panel |
| `src/cli/modes.py` | Add `AUTONOMY_PROMPTS` dict |
| `src/cli/tool_approval_helper.py` | Accept autonomy level, adjust gating logic |
| `src/cli/input_parser.py` | Add `/autonomy` command routing |
| `src/cli/messages/autonomy_info_message.py` | **New** — display current level + list all |
| `src/cli/messages/switch_autonomy_message.py` | **New** — switch autonomy level |
| `tests/cli/autonomy_manager_test.py` | **New** — unit tests |
| `tests/cli/messages/switch_autonomy_message_test.py` | **New** — command tests |
| `tests/cli/tool_approval_helper_test.py` | **New/Update** — approval gating per level |

---

## Implementation Order

Steps 1-3 first (foundation), then 4-6 (core behavior), then 7 (approval changes),
then 8-9 (UX), then 10 (tests). Steps 4 and 6 can be done in parallel. Steps 8 and 9
can be done in parallel.

---

## Out of Scope

- Per-tool autonomy overrides (e.g., "always require approval for shell, regardless of level") — good future enhancement but not needed for v1
- Engagement-level autonomy persistence (resuming an engagement at its original level) — could be added when engagement metadata supports it
- Streaming/partial explanation display — current architecture processes full responses
