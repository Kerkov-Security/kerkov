# Context Window Management - The Stateless Reality

## The Fundamental Truth: LLMs Are Stateless

Every single API call to an LLM (Claude, GPT-4, etc.) is completely independent. The LLM has **zero memory** of previous calls. This means:

```python
# THIS WON'T WORK - The LLM won't remember the first call
response1 = llm.complete("My name is Alice")
response2 = llm.complete("What's my name?")  # LLM has no idea

# THIS WORKS - Send full context every time
messages = [
    {"role": "user", "content": "My name is Alice"},
    {"role": "assistant", "content": "Nice to meet you, Alice!"},
    {"role": "user", "content": "What's my name?"}
]
response = llm.complete(messages)  # LLM can see "Alice" in history
```

## What This Means for Every Query

### Example Conversation Flow

```python
# Query 1
api_call_1 = {
    "messages": [
        {"role": "system", "content": "You are a helpful assistant"},
        {"role": "user", "content": "Help me scan port 80"}
    ]
}
# Tokens sent: ~20

# Query 2 (after LLM responds)
api_call_2 = {
    "messages": [
        {"role": "system", "content": "You are a helpful assistant"},
        {"role": "user", "content": "Help me scan port 80"},
        {"role": "assistant", "content": "I'll scan port 80 for you..."},
        {"role": "tool", "content": "Port 80: open (http)"},
        {"role": "user", "content": "Now scan port 443"}
    ]
}
# Tokens sent: ~80 (includes ALL previous messages)

# Query 3
api_call_3 = {
    "messages": [
        {"role": "system", "content": "You are a helpful assistant"},
        {"role": "user", "content": "Help me scan port 80"},
        {"role": "assistant", "content": "I'll scan port 80 for you..."},
        {"role": "tool", "content": "Port 80: open (http)"},
        {"role": "user", "content": "Now scan port 443"},
        {"role": "assistant", "content": "Scanning port 443..."},
        {"role": "tool", "content": "Port 443: open (https)"},
        {"role": "user", "content": "What did you find?"}
    ]
}
# Tokens sent: ~150 (keeps growing!)
```

## The Cost and Performance Impact

### Token Usage Grows Exponentially

```
Query 1:     20 tokens
Query 2:     80 tokens
Query 3:    150 tokens
Query 4:    300 tokens
Query 5:    500 tokens
Query 10: 2,000 tokens
Query 20: 8,000 tokens
Query 30: 20,000 tokens (approaching limits!)
```

### Real Cost Example (GPT-4 Pricing)
```python
# Assuming $0.03 per 1K input tokens
conversation_length = 10000  # tokens
queries_in_conversation = 20

# Each query sends increasingly more context
total_tokens = sum(range(500, 10000, 500))  # Simplified calculation
# = approximately 95,000 tokens total sent across 20 queries

cost = (95000 / 1000) * 0.03 = $2.85 for one conversation!
```

## Why This Design Exists

1. **Simplicity**: No server-side state management needed
2. **Scalability**: LLM servers don't need session storage
3. **Reliability**: No session state to lose or corrupt
4. **Flexibility**: Clients have full control over context

## Context Window Limits

Each model has a maximum context size:

```python
MODEL_LIMITS = {
    "gpt-4": 8192,              # ~6,000 words
    "gpt-4-32k": 32768,         # ~24,000 words
    "gpt-4-turbo": 128000,      # ~96,000 words
    "claude-3-opus": 200000,    # ~150,000 words
    "claude-3.5-sonnet": 200000,
    "gemini-1.5-pro": 1000000,  # ~750,000 words!
}
```

## Context Management Strategies

### 1. Truncation Strategy
```python
def manage_context(messages, max_tokens=50000):
    """Keep most recent messages within token limit"""

    total_tokens = 0
    kept_messages = []

    # Always keep system prompt
    system_message = messages[0]
    kept_messages.append(system_message)
    total_tokens += estimate_tokens(system_message)

    # Add messages from newest to oldest
    for message in reversed(messages[1:]):
        msg_tokens = estimate_tokens(message)
        if total_tokens + msg_tokens < max_tokens:
            kept_messages.insert(1, message)  # Insert after system
            total_tokens += msg_tokens
        else:
            break

    return kept_messages
```

### 2. Compression Strategy
```python
def compress_context(messages):
    """Summarize old messages to save tokens"""

    if len(messages) < 20:
        return messages

    # Take first 10 messages to summarize
    old_messages = messages[1:11]  # Skip system prompt
    recent_messages = messages[11:]

    # Get LLM to summarize
    summary_prompt = {
        "role": "system",
        "content": "Summarize these messages concisely, keeping key technical details:"
    }

    summary = get_llm_summary(old_messages)

    return [
        messages[0],  # System prompt
        {"role": "system", "content": f"Previous conversation summary: {summary}"},
        *recent_messages
    ]
```

### 3. Importance-Based Retention
```python
def smart_context_management(messages):
    """Keep important messages, drop less important ones"""

    important = []
    regular = []

    for msg in messages:
        # Keep tool outputs (they're usually important)
        if msg['role'] == 'tool':
            important.append(msg)
        # Keep error messages
        elif 'error' in msg.get('content', '').lower():
            important.append(msg)
        # Keep messages with code
        elif '```' in msg.get('content', ''):
            important.append(msg)
        else:
            regular.append(msg)

    # Keep all important + recent regular messages
    return important + regular[-10:]
```

### 4. Conversation Forking
```python
class ConversationManager:
    def __init__(self):
        self.main_context = []
        self.task_contexts = {}  # Separate contexts for subtasks

    def fork_for_task(self, task_name):
        """Create a minimal context for a specific task"""

        # Only include relevant context for this task
        task_context = [
            self.main_context[0],  # System prompt
            {"role": "system", "content": f"Focus on task: {task_name}"},
            # Include only last few messages
            *self.main_context[-3:]
        ]

        self.task_contexts[task_name] = task_context
        return task_context
```

## Optimization Techniques

### 1. Caching Repeated Patterns
```python
class ContextCache:
    def __init__(self):
        self.cache = {}

    def get_cached_response(self, messages_hash):
        """Check if we've seen this exact context before"""
        return self.cache.get(messages_hash)

    def cache_response(self, messages_hash, response):
        self.cache[messages_hash] = response
```

### 2. Lazy Loading Details
```python
def create_minimal_context(messages):
    """Send minimal context, load details only when needed"""

    minimal = []
    for msg in messages:
        if msg['role'] == 'tool' and len(msg['content']) > 1000:
            # Replace long outputs with summaries
            minimal.append({
                'role': 'tool',
                'content': f"[Output truncated - {len(msg['content'])} bytes available]"
            })
        else:
            minimal.append(msg)

    return minimal
```

### 3. Strategic Message Batching
```python
def batch_tool_calls(messages):
    """Combine multiple tool results into single message"""

    batched = []
    tool_buffer = []

    for msg in messages:
        if msg['role'] == 'tool':
            tool_buffer.append(msg['content'])
        else:
            if tool_buffer:
                # Combine all tool results
                combined = {
                    'role': 'tool',
                    'content': '\n---\n'.join(tool_buffer)
                }
                batched.append(combined)
                tool_buffer = []
            batched.append(msg)

    return batched
```

## Real Implementation Example

```python
class OptimizedConversationManager:
    def __init__(self, max_context_tokens=50000):
        self.messages = []
        self.max_tokens = max_context_tokens
        self.token_count = 0

    def add_message(self, role, content):
        """Add message and manage context size"""

        message = {"role": role, "content": content}
        self.messages.append(message)

        # Update token count
        self.token_count += self.estimate_tokens(message)

        # Manage context if getting too large
        if self.token_count > self.max_tokens * 0.8:  # 80% threshold
            self.compress_context()

    def get_context_for_api(self):
        """Get optimized context for API call"""

        # This is what gets sent EVERY TIME
        return self.messages

    def compress_context(self):
        """Compress context when approaching limits"""

        print(f"Context size: {self.token_count} tokens - compressing...")

        # Keep system and last 5 messages intact
        system = self.messages[0]
        recent = self.messages[-5:]
        middle = self.messages[1:-5]

        if len(middle) > 10:
            # Summarize the middle section
            summary = self.summarize_messages(middle)

            self.messages = [
                system,
                {"role": "system", "content": f"[Summarized {len(middle)} messages]: {summary}"},
                *recent
            ]

            # Recalculate tokens
            self.token_count = sum(self.estimate_tokens(m) for m in self.messages)

    def estimate_tokens(self, message):
        """Rough token estimation (1 token ≈ 4 characters)"""
        return len(json.dumps(message)) // 4
```

## The Key Takeaway

**Every single query sends the entire conversation history**. This is why:
- Costs increase with conversation length
- There's a hard limit on conversation size
- Context management is crucial for long conversations
- Efficient prompting saves money
- Summarization becomes necessary for long sessions

This stateless design is both a limitation and a feature - it makes the system simpler and more reliable, but requires careful context management for optimal performance and cost.