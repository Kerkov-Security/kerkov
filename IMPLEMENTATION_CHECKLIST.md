# Implementation Checklist - Critical Technical Details

## Essential Libraries & Dependencies

### Core Requirements
```python
# requirements.txt
anthropic>=0.18.0       # Claude API
openai>=1.12.0          # GPT API
google-generativeai     # Gemini API
python-dotenv           # Environment management
asyncio                 # Async operations
rich                    # Terminal UI
prompt-toolkit          # Better CLI input
pydantic               # Data validation
tiktoken               # Token counting
aiofiles               # Async file operations
docker                 # Tool isolation
```

## Critical Implementation Details

### 1. API Key Management
```python
# NEVER hardcode keys - use environment variables
import os
from dotenv import load_dotenv

load_dotenv()

API_KEYS = {
    'ANTHROPIC_API_KEY': os.getenv('ANTHROPIC_API_KEY'),
    'OPENAI_API_KEY': os.getenv('OPENAI_API_KEY'),
    'GOOGLE_API_KEY': os.getenv('GOOGLE_API_KEY')
}
```

### 2. Streaming Responses (Critical for UX)
```python
# Users expect real-time output, not waiting for complete response
async def stream_response(messages):
    stream = await client.messages.create(
        messages=messages,
        stream=True  # IMPORTANT: Enable streaming
    )

    async for chunk in stream:
        print(chunk.delta.text, end='', flush=True)  # Real-time output
```

### 3. Token Counting (Cost Management)
```python
import tiktoken

def count_tokens(text, model="gpt-4"):
    """Accurate token counting for cost estimation"""
    encoding = tiktoken.encoding_for_model(model)
    return len(encoding.encode(text))

# Track token usage
total_tokens_used = 0
total_cost = 0
```

### 4. Error Handling & Retries
```python
from tenacity import retry, wait_exponential, stop_after_attempt

@retry(
    wait=wait_exponential(min=1, max=60),
    stop=stop_after_attempt(3)
)
async def call_llm_with_retry(messages):
    try:
        return await call_llm(messages)
    except RateLimitError:
        await asyncio.sleep(60)  # Rate limit backoff
        raise
    except APIConnectionError:
        # Handle connection issues
        raise
```

### 5. Tool Response Format (CRITICAL)
```python
# Each provider expects slightly different tool response formats

# OpenAI Format
tool_response = {
    "tool_call_id": "call_abc123",
    "role": "tool",
    "name": "bash_execute",
    "content": "Command output here"
}

# Claude Format
tool_response = {
    "role": "user",  # Claude expects tool results as user messages
    "content": [
        {
            "type": "tool_result",
            "tool_use_id": "toolu_abc123",
            "content": "Command output here"
        }
    ]
}
```

### 6. Subprocess Security (CRITICAL FOR BASH EXECUTION)
```python
import subprocess
import shlex

# NEVER use shell=True with user input
# WRONG - Command injection vulnerability
subprocess.run(user_command, shell=True)  # DANGEROUS!

# RIGHT - Safe execution
cmd_parts = shlex.split(user_command)
subprocess.run(cmd_parts, shell=False, capture_output=True, timeout=30)
```

### 7. Context Window Tracking
```python
MODEL_LIMITS = {
    'gpt-4': 8192,
    'gpt-4-turbo': 128000,
    'claude-3-opus': 200000,
    'claude-3.5-sonnet': 200000
}

def will_fit_in_context(messages, model):
    total_tokens = sum(count_tokens(str(msg)) for msg in messages)
    return total_tokens < MODEL_LIMITS[model] * 0.9  # 90% safety margin
```

### 8. Async Architecture (Performance)
```python
# Use async throughout for better performance
async def main_loop():
    while True:
        user_input = await get_user_input()  # Non-blocking input

        # Process multiple tools in parallel
        tasks = [execute_tool(t) for t in tool_calls]
        results = await asyncio.gather(*tasks)
```

### 9. State Persistence
```python
import pickle
import json

class ConversationState:
    def save(self, filepath):
        # JSON for readability
        with open(f"{filepath}.json", 'w') as f:
            json.dump(self.messages, f)

        # Pickle for complex objects
        with open(f"{filepath}.pkl", 'wb') as f:
            pickle.dump(self, f)

    @classmethod
    def load(cls, filepath):
        with open(f"{filepath}.pkl", 'rb') as f:
            return pickle.load(f)
```

### 10. Docker Isolation for Tools
```python
# Run dangerous tools in containers
import docker

client = docker.from_env()

def run_in_container(command, image="ubuntu:latest"):
    container = client.containers.run(
        image,
        command,
        detach=True,
        mem_limit="512m",
        cpu_quota=50000,  # 50% CPU
        network_mode="none",  # No network for safety
        remove=True
    )
    return container.logs()
```

## Common Pitfalls to Avoid

### 1. Message Format Errors
```python
# WRONG - Will cause API errors
messages = [
    {"role": "user", "content": "Hello"},
    {"role": "user", "content": "Help"}  # Can't have consecutive user messages
]

# RIGHT
messages = [
    {"role": "user", "content": "Hello"},
    {"role": "assistant", "content": "Hi!"},
    {"role": "user", "content": "Help"}
]
```

### 2. Blocking Operations
```python
# WRONG - Blocks entire program
result = subprocess.run(command, timeout=300)  # 5 minute block!

# RIGHT - Async with timeout
result = await asyncio.wait_for(
    run_command_async(command),
    timeout=30
)
```

### 3. Memory Leaks
```python
# WRONG - Unbounded growth
conversation_history = []  # Grows forever

# RIGHT - Bounded history
from collections import deque
conversation_history = deque(maxlen=100)  # Auto-removes old items
```

### 4. Cost Explosion
```python
# WRONG - No cost controls
while True:
    response = expensive_llm_call(entire_history)

# RIGHT - Cost awareness
if estimate_cost(messages) > MAX_COST_PER_QUERY:
    messages = compress_context(messages)
```

## Security Considerations

### Input Validation
```python
def validate_command(cmd):
    BANNED_COMMANDS = ['rm -rf /', 'format', 'del /F /S /Q']
    BANNED_PATTERNS = ['../../', '$(', '`', '&&', '||', ';']

    if any(banned in cmd for banned in BANNED_COMMANDS + BANNED_PATTERNS):
        raise SecurityError("Command contains banned patterns")
```

### Audit Logging
```python
import logging
from datetime import datetime

audit_logger = logging.getLogger('audit')

def log_tool_execution(tool_name, args, user):
    audit_logger.info({
        'timestamp': datetime.utcnow().isoformat(),
        'user': user,
        'tool': tool_name,
        'arguments': args,
        'ip': get_client_ip()
    })
```

## Performance Optimizations

### 1. Connection Pooling
```python
# Reuse HTTP connections
import aiohttp

session = aiohttp.ClientSession(
    connector=aiohttp.TCPConnector(limit=10, limit_per_host=5)
)
```

### 2. Response Caching
```python
from functools import lru_cache
import hashlib

@lru_cache(maxsize=100)
def get_cached_response(messages_hash):
    # Cache identical queries
    return cached_responses.get(messages_hash)
```

### 3. Parallel Tool Execution
```python
async def execute_tools_parallel(tool_calls):
    # Run all tools simultaneously
    tasks = [execute_tool(tc) for tc in tool_calls]
    return await asyncio.gather(*tasks, return_exceptions=True)
```

## Quick Start Template

```python
# main.py - Minimal working example
import asyncio
import os
from anthropic import AsyncAnthropic

async def main():
    client = AsyncAnthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))

    messages = []
    print("Chat started. Type 'quit' to exit.")

    while True:
        # Get user input
        user_input = input("\nYou: ")
        if user_input.lower() == 'quit':
            break

        # Add to messages
        messages.append({"role": "user", "content": user_input})

        # Get response
        print("\nAssistant: ", end="")
        response = await client.messages.create(
            model="claude-3-sonnet-20240229",
            messages=messages,
            max_tokens=1000,
            stream=True
        )

        full_response = ""
        async for chunk in response:
            if chunk.type == 'content_block_delta':
                print(chunk.delta.text, end="", flush=True)
                full_response += chunk.delta.text

        # Add assistant response to history
        messages.append({"role": "assistant", "content": full_response})

        # Basic context management
        if len(messages) > 20:
            messages = messages[-10:]  # Keep last 10

if __name__ == "__main__":
    asyncio.run(main())
```

## You're Ready!

With these technical details, you have everything needed to start building. Begin with:
1. Basic message loop (above template)
2. Add one tool (like bash execution)
3. Implement streaming
4. Add context management
5. Layer in security controls

The architecture is modular - start simple and add complexity as needed!