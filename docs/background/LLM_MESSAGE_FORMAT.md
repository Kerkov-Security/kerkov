# LLM Message Format and Conversation Flow

## How LLMs Understand Message Order

LLMs are trained to understand conversations as **ordered sequences**. The last message in the array is always interpreted as the most recent/current request.

## Standard Message Format (OpenAI/Anthropic/Google)

```python
messages = [
    {"role": "system", "content": "You are a security assistant"},  # Instructions
    {"role": "user", "content": "Scan port 80"},                    # First user input
    {"role": "assistant", "content": "I'll scan port 80..."},       # First LLM response
    {"role": "tool", "content": "Port 80: open"},                   # Tool result
    {"role": "assistant", "content": "Port 80 is open"},            # LLM processes result
    {"role": "user", "content": "Now scan port 443"}                # Current request (LAST)
]

# The LLM understands this is a conversation where:
# 1. User asked about port 80
# 2. Assistant scanned it
# 3. User is NOW asking about port 443
```

## Message Roles Explained

### Standard Roles Across Providers

| Role | OpenAI | Anthropic (Claude) | Google (Gemini) | Purpose |
|------|--------|-------------------|-----------------|----------|
| system | ✅ | ✅ | ✅ (as 'system_instruction') | Set behavior/instructions |
| user | ✅ | ✅ | ✅ | User input |
| assistant | ✅ | ✅ | ✅ (as 'model') | LLM responses |
| tool/function | ✅ (as 'tool') | ✅ (as 'tool_result') | ✅ (as 'function_response') | Tool outputs |

## API Examples for Each Provider

### OpenAI (GPT-4)
```python
import openai

response = openai.ChatCompletion.create(
    model="gpt-4",
    messages=[
        {"role": "system", "content": "You are a helpful assistant"},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi! How can I help?"},
        {"role": "user", "content": "What's 2+2?"}  # Current question
    ]
)
# GPT-4 knows the current question is "What's 2+2?"
```

### Anthropic (Claude)
```python
import anthropic

client = anthropic.Anthropic()
response = client.messages.create(
    model="claude-3-opus-20240229",
    system="You are a helpful assistant",  # System message separate
    messages=[
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi! How can I help?"},
        {"role": "user", "content": "What's 2+2?"}  # Current question
    ]
)
# Claude knows the current question is "What's 2+2?"
```

### Google (Gemini)
```python
import google.generativeai as genai

model = genai.GenerativeModel('gemini-pro')
chat = model.start_chat(history=[
    {"role": "user", "parts": ["Hello"]},
    {"role": "model", "parts": ["Hi! How can I help?"]},
])

response = chat.send_message("What's 2+2?")  # Current question
# Gemini knows the current question is "What's 2+2?"
```

## The Message Order Contract

### What the LLM Sees
```python
messages = [
    {"role": "system", "content": "Instructions"},     # Position 0: Context/Rules
    {"role": "user", "content": "First question"},     # Position 1: Historical
    {"role": "assistant", "content": "First answer"},  # Position 2: Historical
    {"role": "user", "content": "Second question"},    # Position 3: Historical
    {"role": "assistant", "content": "Second answer"}, # Position 4: Historical
    {"role": "user", "content": "Current question"}    # Position N-1: CURRENT REQUEST
]

# The LLM processes this as:
# 1. "Given these instructions..."
# 2. "And this conversation history..."
# 3. "Respond to the LAST user message"
```

### Rules for Message Ordering

1. **Always chronological** - Oldest first, newest last
2. **Alternating pattern** - Usually user → assistant → user → assistant
3. **Last message drives response** - LLM responds to the final message
4. **Tool results inline** - Tool/function results appear immediately after the assistant message that called them

## How LLMs Process the Context

```python
# What happens inside the LLM (conceptually)
def llm_process(messages):
    # 1. Parse system instructions
    instructions = messages[0] if messages[0]['role'] == 'system' else None

    # 2. Build conversation context
    context = messages[:-1]  # Everything except last message

    # 3. Identify current request
    current_request = messages[-1]  # Last message

    # 4. Generate response considering:
    #    - System instructions
    #    - Conversation history
    #    - Current request
    return generate_response(instructions, context, current_request)
```

## Common Patterns

### Pattern 1: Simple Q&A
```python
messages = [
    {"role": "user", "content": "What's the capital of France?"},
    {"role": "assistant", "content": "Paris"},
    {"role": "user", "content": "What about Germany?"}  # Current
]
# LLM understands: User is asking about Germany's capital
```

### Pattern 2: Tool Use Flow
```python
messages = [
    {"role": "user", "content": "Scan the network"},
    {"role": "assistant", "content": "I'll scan the network",
     "tool_calls": [{"function": {"name": "nmap", "arguments": {"target": "192.168.1.0/24"}}}]},
    {"role": "tool", "content": "Found 5 hosts"},
    {"role": "user", "content": "Tell me more about the first host"}  # Current
]
# LLM understands: User wants details about the first of the 5 hosts found
```

### Pattern 3: Multi-Turn Reasoning
```python
messages = [
    {"role": "user", "content": "I need to pentest a web server"},
    {"role": "assistant", "content": "I'll help. What's the target URL?"},
    {"role": "user", "content": "https://example.com"},
    {"role": "assistant", "content": "Let me start with reconnaissance..."},
    {"role": "tool", "content": "Server: nginx/1.18.0"},
    {"role": "assistant", "content": "Found nginx. Checking for vulnerabilities..."},
    {"role": "tool", "content": "No critical vulnerabilities found"},
    {"role": "user", "content": "Check for SQL injection"}  # Current
]
# LLM understands: Continue the pentest, specifically checking for SQLi
```

## Important Nuances

### 1. Message Validation
```python
# Most APIs validate the message structure
def validate_messages(messages):
    # Check alternating pattern
    for i in range(1, len(messages)):
        if messages[i]['role'] == messages[i-1]['role']:
            if messages[i]['role'] not in ['tool', 'system']:
                raise ValueError("Cannot have consecutive user or assistant messages")

    # Ensure last message is from user (for new responses)
    if messages[-1]['role'] != 'user':
        raise ValueError("Last message must be from user")
```

### 2. Tool Response Handling
```python
# Tool responses must follow the assistant message that called them
messages = [
    {"role": "assistant", "content": "Scanning...",
     "tool_calls": [{"id": "call_123", "function": {"name": "scan"}}]},
    {"role": "tool", "tool_call_id": "call_123", "content": "Results..."},  # Must be next
]
```

### 3. System Message Positioning
```python
# System messages usually go first, but some APIs allow multiple
messages = [
    {"role": "system", "content": "You are a security expert"},
    {"role": "system", "content": "Follow ethical guidelines"},  # Additional context
    {"role": "user", "content": "Help me test my server"},
]
```

## The Key Mental Model

Think of it like a chat transcript:
```
System: You are a helpful assistant
User: Hello
Assistant: Hi there!
User: What's 2+2?
Assistant: 4
User: What's 3+3?  <-- CURRENT POSITION
[LLM generates response here]
```

The LLM always:
1. Reads the entire conversation top-to-bottom
2. Understands the last user message as "what to respond to"
3. Generates a response that continues the conversation naturally

## Yes, It's That Simple!

Every major LLM API works this way:
- **Send all messages in order**
- **Last message = current request**
- **LLM understands the flow automatically**

No special markers or flags needed - the position in the array tells the LLM everything about the conversation structure.