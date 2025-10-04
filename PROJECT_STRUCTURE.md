# Project Structure - Kerkov Bot

```
kerkov-bot/
│
├── src/
│   ├── __init__.py
│   │
│   ├── core/                    # Core functionality
│   │   ├── __init__.py
│   │   ├── conversation.py      # Conversation management & context
│   │   ├── llm_controller.py    # LLM API interactions
│   │   ├── message_handler.py   # Message formatting & validation
│   │   ├── stream_handler.py    # Response streaming
│   │   └── state_manager.py     # Session persistence
│   │
│   ├── providers/                # LLM provider implementations
│   │   ├── __init__.py
│   │   ├── base.py              # Abstract base provider
│   │   ├── anthropic_provider.py
│   │   ├── openai_provider.py
│   │   └── google_provider.py
│   │
│   ├── tools/                    # Tool implementations
│   │   ├── __init__.py
│   │   ├── base_tool.py         # Abstract tool class
│   │   ├── bash_tool.py         # Shell command execution
│   │   ├── file_tool.py         # File operations
│   │   ├── web_tool.py          # Web requests
│   │   └── security/            # Security-specific tools
│   │       ├── __init__.py
│   │       ├── nmap_tool.py
│   │       ├── sqlmap_tool.py
│   │       ├── nuclei_tool.py
│   │       └── metasploit_tool.py
│   │
│   ├── execution/                # Tool execution engine
│   │   ├── __init__.py
│   │   ├── executor.py          # Main execution orchestrator
│   │   ├── sandbox.py           # Docker/isolation handler
│   │   ├── validator.py         # Input validation & safety
│   │   └── registry.py          # Tool registry & discovery
│   │
│   ├── context/                  # Context management
│   │   ├── __init__.py
│   │   ├── window_manager.py    # Context window strategies
│   │   ├── compressor.py        # Message compression/summarization
│   │   ├── token_counter.py     # Token counting & estimation
│   │   └── cache.py             # Response caching
│   │
│   ├── cli/                      # CLI interface
│   │   ├── __init__.py
│   │   ├── app.py               # Main CLI application
│   │   ├── input_handler.py     # User input processing
│   │   ├── output_formatter.py  # Response formatting
│   │   ├── commands.py          # Special commands (/help, /clear, etc.)
│   │   └── themes.py            # Terminal themes & colors
│   │
│   ├── security/                 # Security & safety
│   │   ├── __init__.py
│   │   ├── auth.py              # Target authorization
│   │   ├── audit.py             # Audit logging
│   │   ├── rate_limiter.py      # Rate limiting
│   │   ├── scope_manager.py     # Scope validation
│   │   └── sanitizer.py         # Input sanitization
│   │
│   ├── utils/                    # Utilities
│   │   ├── __init__.py
│   │   ├── config.py            # Configuration management
│   │   ├── logger.py            # Logging setup
│   │   ├── errors.py            # Custom exceptions
│   │   ├── helpers.py           # Helper functions
│   │   └── async_utils.py       # Async utilities
│   │
│   └── models/                   # Data models
│       ├── __init__.py
│       ├── message.py           # Message schemas
│       ├── tool_call.py         # Tool call schemas
│       ├── session.py           # Session models
│       └── config.py            # Config schemas
│
├── config/                       # Configuration files
│   ├── default.yaml             # Default configuration
│   ├── tools.yaml               # Tool definitions
│   └── prompts/                 # System prompts
│       ├── default.txt
│       ├── security.txt
│       └── ethical.txt
│
├── tests/                        # Test suite
│   ├── unit/
│   ├── integration/
│   └── fixtures/
│
├── scripts/                      # Utility scripts
│   ├── setup.py                 # Initial setup script
│   └── docker_setup.sh          # Docker environment setup
│
├── data/                        # Data storage
│   ├── sessions/                # Saved sessions
│   ├── logs/                    # Application logs
│   └── cache/                   # Response cache
│
├── .env.example                 # Environment variables template
├── requirements.txt             # Python dependencies
├── docker-compose.yml           # Docker services
├── Dockerfile                   # Container definition
└── README.md                    # Project documentation
```

## Folder Purposes

### `/core`
Heart of the application - manages conversations, LLM interactions, message flow

### `/providers`
Abstracts different LLM providers behind common interface, handles API-specific quirks

### `/tools`
All tool implementations, with security tools in dedicated subfolder

### `/execution`
Orchestrates tool execution, handles sandboxing, validates inputs

### `/context`
Manages context window, implements compression strategies, handles token limits

### `/cli`
User interface layer, handles input/output, special commands

### `/security`
All security controls - authorization, auditing, rate limiting, scope management

### `/utils`
Shared utilities, configuration, logging, helpers

### `/models`
Pydantic models for data validation and schemas