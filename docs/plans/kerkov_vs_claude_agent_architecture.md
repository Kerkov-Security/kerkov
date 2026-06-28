# Kerkov Agent Architecture — Lessons from Claude Code

A comparison of Claude Code's agent architecture with Kerkov's existing codebase, and recommendations for what to adopt when building Kerkov's agent system.

---

## What Kerkov Already Has (Parallels to Claude Code)

`KerkovCLI.get_response()` is already a **tool execution loop** — the same core pattern as Claude Code's `query()`. It:
- Calls the LLM, checks for tool calls, executes them, feeds results back, loops until no more tool calls
- Has a max iteration guard (100)
- Has a permission/approval system (`ToolApprovalCliHelper`)
- Has tools registered via `ToolSuite` with a `BaseTool` ABC

The **foundation is solid**. The core loop does not need to be rewritten.

---

## What to Adopt

### 1. Sub-Agent Spawning (High Value)

Claude Code's `AgentTool` is the most relevant pattern. The key idea:
- An **AgentTool** that the LLM can call like any other tool
- It creates a **new, isolated message history** with just the agent's task prompt
- Uses the **same `get_response()` loop** recursively (not a separate process)
- Returns only the **final text result** to the parent conversation
- **Excludes itself** from the sub-agent's available tools (prevents infinite recursion)
- Uses a **stripped-down system prompt** focused on task completion

This maps cleanly to Kerkov's architecture: create a new `BaseTool` subclass that internally spins up a mini conversation loop with restricted tools.

### 2. Read-Only vs. Stateful Tool Concurrency (Medium Value)

Claude Code runs read-only tools concurrently (up to 10) and stateful tools serially. Kerkov's current loop runs everything serially. Adding an `is_read_only()` method to `BaseTool` and running read-only tools concurrently with `asyncio.gather()` would be a straightforward performance win.

### 3. Restricted Tool Sets for Sub-Agents (High Value)

Sub-agents should get a **subset** of tools. For security work, this is especially important — you might want a "recon agent" that only has `FetchTool`, `ShellTool` (with constraints), and `ReadTool`, but not `WriteTool`.

---

## What NOT to Adopt

### 1. React/Ink as Control Loop
Claude Code uses React for terminal UI. Kerkov has Rich + Click — this is fine and much simpler for a Python CLI. No reason to change.

### 2. Async Generators / Yield-Based Streaming
Claude Code uses `AsyncGenerator` to stream tool progress back to the parent. Kerkov's current approach of synchronous status spinners (`console.status`) is simpler and works well for a security CLI where clear sequential output matters. Sub-agent progress can just be logged.

### 3. Prompt Caching / System Prompt Splitting
This is Anthropic API-specific optimization. Kerkov's multi-provider architecture (Anthropic, Gemini, Ollama) means it can't rely on provider-specific features at the architecture level.

### 4. MCP Tool Integration
Not relevant unless Kerkov plans to connect to external tool servers. The tool suite is self-contained.

### 5. Sidechain Log Files for Sub-Agents
Nice-to-have but not essential. The engagement store already tracks messages — sub-agent conversations can go through the same system if needed.

---

## Recommended Agent Architecture for Kerkov

### File Structure
```
src/agents/
├── __init__.py
├── base_agent.py          # BaseAgent ABC — defines agent interface
├── agent_tool.py          # AgentTool (BaseTool) — LLM calls this to spawn sub-agents
└── agent_runner.py        # Runs the inner conversation loop for a sub-agent
```

### Core Design

1. **`BaseAgent`** — Defines agent type: name, system prompt, allowed tools, description
2. **`AgentTool`** — A `BaseTool` the LLM invokes with `{agent_type, prompt}` params
3. **`AgentRunner`** — Reuses the existing provider + tool loop logic in an isolated context (fresh message history, restricted tools, agent-specific system prompt)

The sub-agent would **not** have access to `AgentTool` itself (no recursive spawning), and would have a configurable tool whitelist per agent type.

### Security-Specific Agent Types

| Agent Type | Tools Available | Purpose |
|---|---|---|
| **Recon Agent** | Shell (constrained), Fetch, Read | Network scanning, OSINT, enumeration |
| **Exploit Agent** | Shell, Write, Read | Exploitation, payload delivery |
| **Analysis Agent** | Read (only) | Log analysis, code review, artifact inspection |

### How It Works (Data Flow)

```
User prompt
  → LLM decides to spawn agent
  → LLM calls AgentTool({agent_type: "recon", prompt: "enumerate services on 10.0.0.5"})
  → AgentRunner creates:
      - Fresh message history (system prompt + task prompt only)
      - Restricted tool set (recon tools only)
      - Same provider instance
  → AgentRunner runs inner tool loop (same logic as get_response)
  → Returns final text result to parent as ToolResult
  → Parent LLM continues with the agent's findings
```

### Key Constraints
- **No recursive spawning** — AgentTool excluded from sub-agent tool sets
- **Tool whitelisting** — Each agent type declares which tools it can use
- **Isolated context** — Sub-agents don't see the parent's conversation history
- **Same process** — Sub-agents are nested async calls, not separate processes
