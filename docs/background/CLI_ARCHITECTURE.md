# Conversational CLI Architecture - How Tools Like Claude Code Work

## Overview
A conversational CLI tool maintains an ongoing dialogue with an LLM while executing actions in the user's environment. The system orchestrates between user input, tool execution, and LLM reasoning in a continuous loop.

## Core Architecture Flow

```
┌──────────────────────────────────────────────────────────────┐
│                         USER                                  │
└────────────────────┬─────────────────────────────────────────┘
                     │ Types command/query
                     ▼
┌──────────────────────────────────────────────────────────────┐
│                    CLI APPLICATION                            │
│  ┌──────────────────────────────────────────────────────┐   │
│  │              Input Handler & Parser                   │   │
│  │  • Captures user input                              │   │
│  │  • Handles special commands (/, quit, etc.)         │   │
│  │  • Maintains input history                          │   │
│  └──────────────────────┬───────────────────────────────┘   │
│                          ▼                                    │
│  ┌──────────────────────────────────────────────────────┐   │
│  │              Conversation Manager                     │   │
│  │  • Maintains conversation history                    │   │
│  │  • Manages context window                           │   │
│  │  • Handles message formatting                       │   │
│  └──────────────────────┬───────────────────────────────┘   │
│                          ▼                                    │
│  ┌──────────────────────────────────────────────────────┐   │
│  │                  LLM Controller                       │   │
│  │  • Constructs API requests                          │   │
│  │  • Manages streaming responses                      │   │
│  │  • Handles function calling                         │   │
│  └──────────────────────┬───────────────────────────────┘   │
└──────────────────────────┼───────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────┐
│                    LLM API (Claude/GPT)                       │
│  • Processes conversation context                             │
│  • Decides on tool usage                                      │
│  • Generates responses                                        │
└────────────────────┬─────────────────────────────────────────┘
                     │ Returns response + function calls
                     ▼
┌──────────────────────────────────────────────────────────────┐
│                 TOOL EXECUTION ENGINE                         │
│  ┌──────────────────────────────────────────────────────┐   │
│  │              Function Call Parser                     │   │
│  │  • Extracts tool name and parameters                │   │
│  │  • Validates against tool registry                  │   │
│  └──────────────────────┬───────────────────────────────┘   │
│                          ▼                                    │
│  ┌──────────────────────────────────────────────────────┐   │
│  │                Tool Registry                          │   │
│  │  • Maps tool names to implementations               │   │
│  │  • Defines tool schemas/parameters                  │   │
│  │  • Handles permissions and safety checks            │   │
│  └──────────────────────┬───────────────────────────────┘   │
│                          ▼                                    │
│  ┌──────────────────────────────────────────────────────┐   │
│  │              Tool Implementations                     │   │
│  │  • File operations (read/write/edit)                │   │
│  │  • Shell command execution                          │   │
│  │  • Web requests                                     │   │
│  │  • Security tools (nmap, sqlmap, etc.)             │   │
│  └──────────────────────┬───────────────────────────────┘   │
└──────────────────────────┼───────────────────────────────────┘
                           │ Tool results
                           ▼
┌──────────────────────────────────────────────────────────────┐
│                    FEEDBACK LOOP                              │
│  • Tool results added to conversation                         │
│  • LLM processes results and continues                        │
│  • May trigger additional tool calls                          │
└────────────────────────────────────────────────────────────┘
```

## Detailed Component Breakdown

### 1. Input Layer
```python
# Simplified example of input handling
class InputHandler:
    def __init__(self):
        self.history = []
        self.commands = {
            '/help': self.show_help,
            '/clear': self.clear_context,
            '/model': self.switch_model
        }

    def get_input(self):
        user_input = input(">>> ")

        # Handle special commands
        if user_input.startswith('/'):
            return self.handle_command(user_input)

        # Regular message
        self.history.append(user_input)
        return {'type': 'message', 'content': user_input}
```

### 2. Conversation Management
The conversation manager maintains the entire dialogue history and manages the context window:

```python
class ConversationManager:
    def __init__(self, max_context_length=100000):
        self.messages = []
        self.max_context = max_context_length
        self.system_prompt = self.load_system_prompt()

    def add_message(self, role, content, tool_calls=None):
        message = {
            'role': role,  # 'user', 'assistant', 'tool'
            'content': content,
            'timestamp': datetime.now()
        }
        if tool_calls:
            message['tool_calls'] = tool_calls

        self.messages.append(message)
        self.truncate_if_needed()

    def truncate_if_needed(self):
        """Context window management strategies"""
        total_tokens = self.estimate_tokens()

        if total_tokens > self.max_context:
            # Strategy 1: Remove oldest messages
            # Strategy 2: Summarize old conversations
            # Strategy 3: Keep only recent + important context
            pass

    def get_context_for_llm(self):
        """Prepare messages for LLM API"""
        return [
            {'role': 'system', 'content': self.system_prompt},
            *self.messages
        ]
```

### 3. LLM Integration with Function Calling

Modern LLMs support "function calling" or "tool use" - a structured way for the model to request actions:

```python
class LLMController:
    def __init__(self, api_key, model="claude-3-opus"):
        self.client = Anthropic(api_key=api_key)
        self.model = model
        self.tools = self.load_tool_definitions()

    def get_completion(self, messages):
        response = self.client.messages.create(
            model=self.model,
            messages=messages,
            tools=self.tools,  # Tool definitions
            max_tokens=4096,
            stream=True  # For real-time output
        )

        return self.process_response(response)

    def load_tool_definitions(self):
        """Define available tools in OpenAI/Anthropic format"""
        return [
            {
                "name": "read_file",
                "description": "Read contents of a file",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path"},
                        "start_line": {"type": "integer", "optional": True},
                        "end_line": {"type": "integer", "optional": True}
                    },
                    "required": ["path"]
                }
            },
            {
                "name": "execute_command",
                "description": "Execute a shell command",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "timeout": {"type": "integer", "default": 30}
                    },
                    "required": ["command"]
                }
            }
            # ... more tools
        ]
```

### 4. Tool Execution Flow

When the LLM decides to use a tool, it returns structured data:

```python
# Example LLM response with tool use
llm_response = {
    "content": "I'll check what files are in the current directory.",
    "tool_calls": [
        {
            "id": "call_abc123",
            "type": "function",
            "function": {
                "name": "execute_command",
                "arguments": {"command": "ls -la"}
            }
        }
    ]
}

class ToolExecutor:
    def __init__(self):
        self.tools = {
            'read_file': self.read_file,
            'execute_command': self.execute_command,
            'run_security_scan': self.run_security_scan
        }

    async def execute_tool_call(self, tool_call):
        tool_name = tool_call['function']['name']
        arguments = json.loads(tool_call['function']['arguments'])

        # Safety checks
        if not self.validate_tool_call(tool_name, arguments):
            return {"error": "Tool call validation failed"}

        # Execute tool
        try:
            result = await self.tools[tool_name](**arguments)
            return {
                "tool_call_id": tool_call['id'],
                "result": result
            }
        except Exception as e:
            return {
                "tool_call_id": tool_call['id'],
                "error": str(e)
            }

    async def execute_command(self, command, timeout=30):
        """Sandboxed command execution"""
        # Use subprocess with proper isolation
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Add security constraints
            preexec_fn=lambda: os.setrlimit(resource.RLIMIT_CPU, (timeout, timeout))
        )

        stdout, stderr = await process.communicate()
        return {
            "stdout": stdout.decode(),
            "stderr": stderr.decode(),
            "exit_code": process.returncode
        }
```

## Context Management Strategies

### 1. Sliding Window
Keep only the most recent N messages:
```python
def sliding_window(messages, max_messages=50):
    if len(messages) <= max_messages:
        return messages
    return messages[-max_messages:]
```

### 2. Importance-Based Retention
Keep important messages (errors, key decisions):
```python
def importance_based(messages):
    important = []
    recent = []

    for msg in messages:
        if msg.get('importance') == 'high' or 'error' in msg.get('content', ''):
            important.append(msg)
        else:
            recent.append(msg)

    # Keep all important + recent within token limit
    return important + recent[-20:]
```

### 3. Summarization
Periodically summarize old conversations:
```python
async def summarize_old_context(messages):
    old_messages = messages[:-10]  # Keep last 10 intact

    summary_prompt = "Summarize the key points and decisions from this conversation"
    summary = await get_llm_summary(old_messages, summary_prompt)

    return [{
        'role': 'system',
        'content': f"Previous conversation summary: {summary}"
    }] + messages[-10:]
```

## Streaming and Real-time Output

For better UX, responses are streamed:

```python
class StreamHandler:
    def __init__(self):
        self.buffer = ""
        self.in_code_block = False

    async def handle_stream(self, response_stream):
        async for chunk in response_stream:
            if chunk.type == 'content_block_delta':
                self.buffer += chunk.delta.text

                # Detect and format code blocks
                if '```' in chunk.delta.text:
                    self.in_code_block = not self.in_code_block

                # Print with appropriate formatting
                if self.in_code_block:
                    print(colored(chunk.delta.text, 'cyan'), end='')
                else:
                    print(chunk.delta.text, end='')

            elif chunk.type == 'tool_call':
                # Handle tool execution
                await self.execute_and_stream_tool(chunk)
```

## State Persistence

Conversations and context need to be saved:

```python
class StatePersistence:
    def __init__(self, session_dir=".sessions"):
        self.session_dir = Path(session_dir)
        self.session_dir.mkdir(exist_ok=True)
        self.current_session = None

    def save_session(self, conversation_manager):
        session_data = {
            'messages': conversation_manager.messages,
            'metadata': {
                'timestamp': datetime.now().isoformat(),
                'model': conversation_manager.model,
                'total_tokens': conversation_manager.total_tokens_used
            }
        }

        session_file = self.session_dir / f"session_{self.current_session}.json"
        with open(session_file, 'w') as f:
            json.dump(session_data, f, default=str)

    def load_session(self, session_id):
        session_file = self.session_dir / f"session_{session_id}.json"
        if session_file.exists():
            with open(session_file, 'r') as f:
                return json.load(f)
        return None
```

## Complete Request Flow Example

1. **User Input**: "Find all SQL files and check them for SQL injection vulnerabilities"

2. **CLI processes input** → Adds to conversation history

3. **Sends to LLM** with context and available tools

4. **LLM responds** with reasoning + tool calls:
   ```json
   {
     "content": "I'll search for SQL files and analyze them for injection vulnerabilities.",
     "tool_calls": [
       {"function": {"name": "execute_command", "arguments": {"command": "find . -name '*.sql'"}}}
     ]
   }
   ```

5. **Tool executes**, returns file list

6. **Results sent back to LLM** as tool response message

7. **LLM continues** with next steps:
   ```json
   {
     "content": "Found 5 SQL files. Now analyzing each for vulnerabilities...",
     "tool_calls": [
       {"function": {"name": "read_file", "arguments": {"path": "./queries/user_auth.sql"}}}
     ]
   }
   ```

8. **Cycle continues** until task complete

This architecture enables fluid conversation while maintaining context, executing tools safely, and providing real-time feedback to the user.