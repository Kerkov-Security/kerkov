# Kerkov - Project Overview

## Table of Contents
1. [Introduction](#introduction)
2. [What is an LLM and How Does it Work?](#what-is-an-llm-and-how-does-it-work)
3. [High-Level Architecture](#high-level-architecture)
4. [Component Breakdown](#component-breakdown)
5. [How the System Works](#how-the-system-works)
6. [Data Flow Example](#data-flow-example)

---

## Introduction

**Kerkov** is a security-focused conversational AI assistant designed for penetration testing and security analysis. Think of it as a specialized chatbot (similar to Claude Code or ChatGPT) that helps security professionals with their work by providing intelligent assistance and the ability to execute security-related commands.

The bot operates in three different modes:
- **Defense Mode**: Blue team operations (protecting systems)
- **Audit Mode**: Balanced penetration testing (finding vulnerabilities)
- **Offensive Mode**: Red team operations (simulating attackers)

---

## What is an LLM and How Does it Work?

### Understanding Large Language Models (LLMs)

An **LLM (Large Language Model)** is an AI system trained on vast amounts of text data that can understand and generate human-like text. Think of it as a very sophisticated autocomplete system that can:

1. **Understand Context**: It reads your messages and understands what you're asking
2. **Generate Responses**: It creates human-like responses based on its training
3. **Follow Instructions**: It can follow system prompts that define how it should behave
4. **Use Tools**: Modern LLMs can decide when to use external tools (like running commands)

### How Chatbots Like Claude Code Work

```
┌─────────────────────────────────────────────────────────────────┐
│                    HOW CHATBOTS WORK                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  1. USER INPUT                                                  │
│     "Run ls -la in the current directory"                       │
│                           ↓                                     │
│                                                                 │
│  2. SYSTEM PROMPT (Hidden Instructions)                         │
│     "You are a helpful assistant. When users ask you to        │
│      run commands, use the shell tool."                         │
│                           ↓                                     │
│                                                                 │
│  3. LLM PROCESSES (AI Thinks)                                   │
│     - Reads user input                                          │
│     - Reads system instructions                                 │
│     - Decides what to do                                        │
│                           ↓                                     │
│                                                                 │
│  4. LLM DECISION                                                │
│     "I should use the shell tool with command 'ls -la'"         │
│                           ↓                                     │
│                                                                 │
│  5. TOOL EXECUTION                                              │
│     Shell tool runs: ls -la                                     │
│     Result: [file listing]                                      │
│                           ↓                                     │
│                                                                 │
│  6. LLM RECEIVES RESULT                                         │
│     Tool output is sent back to the LLM                         │
│                           ↓                                     │
│                                                                 │
│  7. FINAL RESPONSE                                              │
│     "Here are the files in the current directory:              │
│      - file1.txt                                                │
│      - file2.py"                                                │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Key Concepts

- **Messages**: The conversation history (user messages and assistant responses)
- **System Prompt**: Hidden instructions that tell the AI how to behave
- **Tools**: External capabilities the AI can use (like running shell commands)
- **Tool Calling**: When the AI decides to use a tool instead of just responding with text
- **Context Window**: The amount of conversation history the AI can "remember"

---

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                           KERKOV SYSTEM                             │
│                                                                     │
│  ┌──────────────┐         ┌──────────────┐      ┌──────────────┐  │
│  │              │         │              │      │              │  │
│  │     CLI      │────────▶│  Mode        │      │   Config     │  │
│  │  Interface   │         │  Manager     │      │   Manager    │  │
│  │              │         │              │      │              │  │
│  └──────┬───────┘         └──────────────┘      └──────────────┘  │
│         │                                                           │
│         │                                                           │
│         ▼                                                           │
│  ┌──────────────────────────────────────────────────────────┐     │
│  │              CONVERSATION ORCHESTRATOR                   │     │
│  │  (Manages message flow and tool execution loop)          │     │
│  └────────┬─────────────────────────────────┬───────────────┘     │
│           │                                 │                      │
│           ▼                                 ▼                      │
│  ┌────────────────┐              ┌──────────────────┐            │
│  │  LLM Provider  │              │     Tools        │            │
│  │  (Gemini API)  │              │  - ExecTool      │            │
│  │                │              │  - (Future)      │            │
│  └────────────────┘              └──────────────────┘            │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Component Breakdown

### 1. CLI Interface (`src/kerkov/cli/`)

**Purpose**: This is what the user interacts with - the command-line interface.

**Files**:
- `main.py`: Entry point - parses command-line arguments and starts the bot
- `kerkov_cli.py`: Main CLI class - handles the conversation loop
- `provider_setup_extension.py`: Provider onboarding and credential prompt workflow
- `mode_manager.py`: Manages switching between Defense/Audit/Offensive modes
- `modes.py`: Defines the three operational modes and their system prompts

**What it does**:
```
┌─────────────────────────────────────────────────────────┐
│                    CLI INTERFACE                        │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  User Types: "scan the network for open ports"          │
│       ↓                                                 │
│  CLI captures input                                     │
│       ↓                                                 │
│  Adds to conversation history                           │
│       ↓                                                 │
│  Sends to LLM Provider                                  │
│       ↓                                                 │
│  Receives response                                      │
│       ↓                                                 │
│  Displays formatted response to user                    │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

**Key Features**:
- Interactive prompt with mode indicator (D/A/O)
- Commands: `/mode`, `/q`, `clear`
- Rich formatting (colors, markdown, panels)
- Conversation history management

---

### 2. Mode System (`src/kerkov/cli/modes.py`)

**Purpose**: Defines different "personalities" for the bot based on security role.

**The Three Modes**:

```
┌────────────────────────────────────────────────────────────────┐
│                         MODES                                  │
├────────────────────────────────────────────────────────────────┤
│                                                                │
│  DEFENSE MODE (Blue Team)                                      │
│  ┌──────────────────────────────────────────────────┐         │
│  │ Focus: Protecting systems                        │         │
│  │ Color: Blue                                       │         │
│  │ System Prompt: "You are a security defense       │         │
│  │                 specialist..."                    │         │
│  │ Behavior: Conservative, focus on fixes           │         │
│  └──────────────────────────────────────────────────┘         │
│                                                                │
│  AUDIT MODE (Balanced) - DEFAULT                               │
│  ┌──────────────────────────────────────────────────┐         │
│  │ Focus: Finding vulnerabilities professionally    │         │
│  │ Color: Magenta                                    │         │
│  │ System Prompt: "You are a professional pentester"│         │
│  │ Behavior: Balanced, methodical testing           │         │
│  └──────────────────────────────────────────────────┘         │
│                                                                │
│  OFFENSIVE MODE (Red Team)                                     │
│  ┌──────────────────────────────────────────────────┐         │
│  │ Focus: Aggressive exploitation                    │         │
│  │ Color: Red                                        │         │
│  │ System Prompt: "You are an elite red team        │         │
│  │                 operator..."                      │         │
│  │ Behavior: Aggressive, creative attacks           │         │
│  └──────────────────────────────────────────────────┘         │
│                                                                │
└────────────────────────────────────────────────────────────────┘
```

**How System Prompts Work**:

Each mode has a **system prompt** - a large set of instructions that tells the AI how to behave. The AI never sees the mode name, but it follows these instructions:

- Defense mode system prompt emphasizes security hardening and remediation
- Audit mode system prompt emphasizes methodology and documentation
- Offensive mode system prompt emphasizes achieving objectives and stealth

When you switch modes, the conversation history is cleared because the AI now has different instructions.

---

### 3. LLM Provider (`src/kerkov/providers/`)

**Purpose**: Communicates with the AI service (Google Gemini) to get intelligent responses.

**Files**:
- `base_provider.py`: Abstract interface that any LLM provider must implement
- `gemini_provider.py`: Implementation for Google Gemini API

```
┌─────────────────────────────────────────────────────────────┐
│                    LLM PROVIDER FLOW                        │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  1. Receive Messages                                        │
│     [                                                       │
│       {role: "system", content: "You are..."},             │
│       {role: "user", content: "scan the network"},         │
│     ]                                                       │
│                    ↓                                        │
│  2. Convert to Gemini Format                                │
│     (Gemini uses "model" instead of "assistant")           │
│                    ↓                                        │
│  3. Add Tool Definitions                                    │
│     "You can use these tools: shell"                        │
│                    ↓                                        │
│  4. Send to Gemini API                                      │
│     → HTTPS Request to Google                               │
│                    ↓                                        │
│  5. Receive Response                                        │
│     Either:                                                 │
│     - Text response: "I'll scan the network..."            │
│     - Tool call: {tool: "shell", params: {...}}            │
│                    ↓                                        │
│  6. Parse and Return                                        │
│     Return LLMResponse object                               │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**What's in an LLM Response?**:
- `content`: The text the AI wants to say
- `tool_calls`: List of tools the AI wants to use (if any)
- `usage`: Token counts (how much of the AI's "thinking" was used)
- `model`: Which AI model responded

---

### 4. Tools System (`src/kerkov/tools/`)

**Purpose**: Gives the AI the ability to perform actions (not just talk).

**Files**:
- `base_tool.py`: Abstract interface for all tools
- `exec_tool.py`: Runs one-shot and background commands via the process supervisor

```
┌──────────────────────────────────────────────────────────────┐
│                      TOOL SYSTEM                             │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  BaseTool (Abstract)                                         │
│  ┌────────────────────────────────────────────┐            │
│  │ - name: str                                │            │
│  │ - description: str                         │            │
│  │ - execute(**params) → ToolResult           │            │
│  │ - validate_params(**params) → bool         │            │
│  └────────────────────────────────────────────┘            │
│                     ↑                                        │
│                     │ implements                            │
│                     │                                        │
│  ExecTool                                                    │
│  ┌────────────────────────────────────────────┐            │
│  │ name: "exec"                               │            │
│  │ description: "Execute shell commands..."   │            │
│  │                                            │            │
│  │ execute(command: str):                     │            │
│  │   1. Validate command                      │            │
│  │   2. Check sandbox mode                    │            │
│  │   3. Run command in subprocess             │            │
│  │   4. Return ToolResult                     │            │
│  └────────────────────────────────────────────┘            │
│                                                              │
│  ToolResult                                                  │
│  ┌────────────────────────────────────────────┐            │
│  │ - success: bool                            │            │
│  │ - output: str                              │            │
│  │ - error: Optional[str]                     │            │
│  │ - metadata: Optional[dict]                 │            │
│  └────────────────────────────────────────────┘            │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

**How Tools Work**:

1. **Tool Definition**: When the LLM provider is initialized, it's given a list of available tools
2. **Tool Description**: Each tool has a description that the AI reads (e.g., "Execute shell commands")
3. **AI Decision**: The AI reads the user request and decides if it needs to use a tool
4. **Tool Call**: AI returns a tool call instead of text: `{tool: "shell", params: {command: "ls"}}`
5. **Execution**: The CLI executes the tool and gets the result
6. **Result to AI**: The tool result is sent back to the AI
7. **Final Response**: The AI reads the result and responds to the user

---

### 5. Configuration System (`src/kerkov/core/config_manager.py`)

**Purpose**: Separates ordinary settings from runtime credentials.

```
┌─────────────────────────────────────────────────────────┐
│                  CONFIGURATION                          │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  .env File (non-secret settings)                        │
│  ┌───────────────────────────────────────┐            │
│  │ LLM_PROVIDER=gemini                   │            │
│  │ DEFAULT_GEMINI_MODEL=gemini-2.5-flash │            │
│  │ EXECUTION_ENABLED=true                │            │
│  │ WRITE_TOOL_ENABLED=true               │            │
│  │ MAX_TIMEOUT_SECONDS=60                │            │
│  └───────────────────────────────────────┘            │
│              ↓ ConfigManager                            │
│                                                         │
│  Masked prompt / environment fallback                   │
│              ↓ CredentialManager                        │
│  Session memory + keyring/protected user file           │
│                                                         │
│  Settings:                                              │
│  • Model settings (which AI model to use)              │
│  • Security settings (capabilities, timeouts)          │
│  • Logging configuration                               │
│  Credentials: provider and web-search API keys          │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

**Key Settings**:
- `/auth set gemini`: Securely collect and save the Gemini API key
- `EXECUTION_ENABLED`: Enables `exec`, `container_lifecycle`, `sbom_generate`,
  `vulnerability_scan`, and `sast_scan`; defaults to true
- `WRITE_TOOL_ENABLED`: Explicitly enables the `write` tool; defaults to true
- `MAX_TIMEOUT_SECONDS`: How long to wait for commands
- `DEFAULT_GEMINI_MODEL`: Which Gemini model to use

---

### 6. Data Models (`src/kerkov/models/basic_models.py`)

**Purpose**: Defines the data structures used throughout the application.

```
┌──────────────────────────────────────────────────────────┐
│                    DATA MODELS                           │
├──────────────────────────────────────────────────────────┤
│                                                          │
│  Message                                                 │
│  ┌────────────────────────────────────┐                │
│  │ role: "user" | "assistant" | "system" │             │
│  │ content: str                       │                │
│  └────────────────────────────────────┘                │
│  Example: {role: "user", content: "Hello"}              │
│                                                          │
│  ToolCall                                                │
│  ┌────────────────────────────────────┐                │
│  │ tool_name: str                     │                │
│  │ parameters: dict                   │                │
│  └────────────────────────────────────┘                │
│  Example: {tool_name: "exec", parameters: {...}}        │
│                                                          │
│  LLMResponse                                             │
│  ┌────────────────────────────────────┐                │
│  │ content: str                       │                │
│  │ model: str                         │                │
│  │ tool_calls: List[ToolCall]         │                │
│  │ usage: TokenUsage                  │                │
│  └────────────────────────────────────┘                │
│                                                          │
│  ToolResult                                              │
│  ┌────────────────────────────────────┐                │
│  │ success: bool                      │                │
│  │ output: str                        │                │
│  │ error: Optional[str]               │                │
│  └────────────────────────────────────┘                │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

These are simple data containers that ensure consistent data structures across the application.

---

## How the System Works

### The Conversation Loop

This is the heart of Kerkov - how it processes user input and responds:

```
┌───────────────────────────────────────────────────────────────────┐
│                    CONVERSATION LOOP                              │
│                  (kerkov_cli.py:144-230)                          │
├───────────────────────────────────────────────────────────────────┤
│                                                                   │
│  START                                                            │
│    ↓                                                              │
│  ┌─────────────────────────────────────────────────┐            │
│  │ 1. User types message                           │            │
│  │    "scan localhost for open ports"              │            │
│  └─────────────────────────────────────────────────┘            │
│    ↓                                                              │
│  ┌─────────────────────────────────────────────────┐            │
│  │ 2. Add to conversation history                  │            │
│  │    history.append(Message(role="user", ...))    │            │
│  └─────────────────────────────────────────────────┘            │
│    ↓                                                              │
│  ┌─────────────────────────────────────────────────┐            │
│  │ 3. Prepare messages (add system prompt)         │            │
│  │    [system prompt, ...history]                  │            │
│  └─────────────────────────────────────────────────┘            │
│    ↓                                                              │
│  ┌─────────────────────────────────────────────────┐            │
│  │ 4. Send to LLM Provider                         │            │
│  │    response = await provider.complete(...)      │            │
│  └─────────────────────────────────────────────────┘            │
│    ↓                                                              │
│  ┌─────────────────────────────────────────────────┐            │
│  │ 5. Check response type                          │            │
│  └─────────────────────────────────────────────────┘            │
│    ↓                            ↓                                │
│    TEXT RESPONSE                TOOL CALL                        │
│    ↓                            ↓                                │
│  ┌──────────────┐         ┌─────────────────────────┐          │
│  │ Display text │         │ Execute tool            │          │
│  │ to user      │         │   ↓                     │          │
│  │              │         │ Get result              │          │
│  │              │         │   ↓                     │          │
│  │              │         │ Add result to messages  │          │
│  │              │         │   ↓                     │          │
│  │              │         │ LOOP BACK TO STEP 4     │          │
│  │              │         │ (AI reads result)       │          │
│  └──────────────┘         └─────────────────────────┘          │
│    ↓                                                              │
│  END (Wait for next user input)                                 │
│                                                                   │
└───────────────────────────────────────────────────────────────────┘
```

### Why the Loop?

The loop (max 5 iterations) allows the AI to:
1. Decide to use a tool
2. See the tool's result
3. Decide what to do next (use another tool or respond to user)

This enables complex workflows like:
- "Scan the network" → AI runs nmap → AI reads results → AI summarizes findings to user

---

## Data Flow Example

Let's trace a complete example: **User asks to list files**

```
┌────────────────────────────────────────────────────────────────────┐
│            COMPLETE FLOW: "list files in the current directory"    │
├────────────────────────────────────────────────────────────────────┤
│                                                                    │
│ STEP 1: USER INPUT                                                 │
│ ┌────────────────────────────────────────────────────────┐        │
│ │ User types: "list files in the current directory"      │        │
│ └────────────────────────────────────────────────────────┘        │
│                                                                    │
│ STEP 2: CLI PROCESSES                                              │
│ ┌────────────────────────────────────────────────────────┐        │
│ │ KerkovCLI._get_response()                              │        │
│ │ • Adds message to history                              │        │
│ │ • Prepares messages with system prompt                 │        │
│ │                                                         │        │
│ │ Messages to send:                                       │        │
│ │ [                                                       │        │
│ │   {role: "system", content: "You are Kerkov...         │        │
│ │    You have access to shell commands..."},             │        │
│ │   {role: "user", content: "list files in the           │        │
│ │    current directory"}                                  │        │
│ │ ]                                                       │        │
│ └────────────────────────────────────────────────────────┘        │
│                          ↓                                         │
│ STEP 3: SEND TO LLM                                                │
│ ┌────────────────────────────────────────────────────────┐        │
│ │ GeminiProvider.complete()                              │        │
│ │ • Converts messages to Gemini format                   │        │
│ │ • Includes tool definitions (shell tool)               │        │
│ │ • Sends HTTP request to Google Gemini API              │        │
│ └────────────────────────────────────────────────────────┘        │
│                          ↓                                         │
│ STEP 4: GEMINI AI THINKS                                           │
│ ┌────────────────────────────────────────────────────────┐        │
│ │ Google's AI processes:                                 │        │
│ │ • Reads system prompt (knows it can use shell tool)    │        │
│ │ • Reads user message                                   │        │
│ │ • Decides: "I should use the shell tool to run ls"     │        │
│ │                                                         │        │
│ │ Returns tool call:                                      │        │
│ │ {                                                       │        │
│ │   tool_name: "exec",                                   │        │
│ │   parameters: {command: "ls -la"}                      │        │
│ │ }                                                       │        │
│ └────────────────────────────────────────────────────────┘        │
│                          ↓                                         │
│ STEP 5: CLI EXECUTES TOOL                                          │
│ ┌────────────────────────────────────────────────────────┐        │
│ │ KerkovCLI detects tool call                            │        │
│ │ • Finds ExecTool instance                              │        │
│ │ • Calls ExecTool.execute(command="ls -la")             │        │
│ │                                                         │        │
│ │ ExecTool:                                               │        │
│ │ • Validates command                                     │        │
│ │ • Checks sandbox mode                                  │        │
│ │ • Runs subprocess: ls -la                              │        │
│ │ • Captures output                                      │        │
│ │                                                         │        │
│ │ Returns:                                                │        │
│ │ ToolResult(                                             │        │
│ │   success: true,                                        │        │
│ │   output: "total 48\ndrwxr-xr-x ... file1.txt\n..."   │        │
│ │ )                                                       │        │
│ └────────────────────────────────────────────────────────┘        │
│                          ↓                                         │
│ STEP 6: SEND RESULT BACK TO AI                                     │
│ ┌────────────────────────────────────────────────────────┐        │
│ │ CLI formats tool result and adds to messages:          │        │
│ │                                                         │        │
│ │ New message:                                            │        │
│ │ {                                                       │        │
│ │   role: "user",                                        │        │
│ │   content: "Tool 'shell' execution result:\n           │        │
│ │             Status: Success\n                           │        │
│ │             Output:\ntotal 48\ndrwxr-xr-x..."          │        │
│ │ }                                                       │        │
│ │                                                         │        │
│ │ Sends back to Gemini (LOOP ITERATION 2)                │        │
│ └────────────────────────────────────────────────────────┘        │
│                          ↓                                         │
│ STEP 7: AI READS RESULT AND RESPONDS                               │
│ ┌────────────────────────────────────────────────────────┐        │
│ │ Gemini receives tool result                            │        │
│ │ • Reads the command output                             │        │
│ │ • Decides to respond to user (no more tools needed)    │        │
│ │                                                         │        │
│ │ Returns text response:                                  │        │
│ │ {                                                       │        │
│ │   content: "Here are the files in the current          │        │
│ │            directory:\n\n- file1.txt\n- file2.py\n..." │        │
│ │   tool_calls: null                                     │        │
│ │ }                                                       │        │
│ └────────────────────────────────────────────────────────┘        │
│                          ↓                                         │
│ STEP 8: DISPLAY TO USER                                            │
│ ┌────────────────────────────────────────────────────────┐        │
│ │ CLI detects text response (no tool calls)              │        │
│ │ • Adds to conversation history                         │        │
│ │ • Formats as markdown                                  │        │
│ │ • Displays to user with Rich formatting                │        │
│ │                                                         │        │
│ │ User sees:                                              │        │
│ │ ┌──────────────────────────────────────────────┐      │        │
│ │ │ Here are the files in the current directory: │      │        │
│ │ │                                               │      │        │
│ │ │ - file1.txt                                   │      │        │
│ │ │ - file2.py                                    │      │        │
│ │ │ ...                                           │      │        │
│ │ └──────────────────────────────────────────────┘      │        │
│ └────────────────────────────────────────────────────────┘        │
│                          ↓                                         │
│ STEP 9: WAIT FOR NEXT INPUT                                        │
│ ┌────────────────────────────────────────────────────────┐        │
│ │ CLI shows prompt: [A] You:                             │        │
│ │ (Ready for next user message)                          │        │
│ └────────────────────────────────────────────────────────┘        │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
```

---

## Summary

### What Makes Kerkov Work

1. **User Interface (CLI)**: Captures user input and displays responses
2. **Mode System**: Changes the AI's behavior based on security role
3. **LLM Provider**: Communicates with Google Gemini AI to get intelligent responses
4. **Tool System**: Allows the AI to execute actions (like shell commands)
5. **Conversation Loop**: Orchestrates the back-and-forth between user, AI, and tools
6. **Configuration**: Manages settings and security controls

### Key Innovations

- **Mode-based system prompts**: The AI behaves differently based on mode without user needing to know
- **Tool execution loop**: The AI can use tools, see results, and act on them
- **Security controls**: Sandbox mode, timeouts, and validation protect the system
- **Extensible architecture**: Easy to add new tools and LLM providers

### Current State

**Working**:
- CLI interface with rich formatting
- Three operational modes (Defense, Audit, Offensive)
- Google Gemini integration
- Shell command execution with safety controls
- Conversation history management

**In Development** (based on git status):
- Enhanced mode management system
- Additional CLI features

**Future Potential**:
- More tools (nmap and additional ZAP automation, etc.)
- Additional LLM providers (OpenAI, Anthropic)
- Session persistence
- Collaborative features
- Reporting capabilities

---

## For Your Project Partner

This system is essentially a specialized chatbot for security work. The key differences from general chatbots like ChatGPT:

1. **Mode-specific behavior**: The AI follows different instructions based on security context
2. **Tool integration**: It can actually execute commands, not just suggest them
3. **Security focus**: All prompts and tools are designed for penetration testing
4. **Safety controls**: Sandbox mode and other protections prevent dangerous operations

The architecture is clean and extensible - adding new capabilities (tools, modes, providers) follows clear patterns established by the base classes.
