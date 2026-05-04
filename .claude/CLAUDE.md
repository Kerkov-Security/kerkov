# Kerkov Bot

Security-focused conversational AI CLI for authorized penetration testing, security auditing, and CTF competitions. Integrates with LLM providers (Claude, Gemini, Ollama) to provide intelligent security analysis with tool execution capabilities.

## Project Structure
```
src/
├── cli/           # Entry point (main.py), conversation loop (kerkov_cli.py), modes, input parsing
├── providers/     # LLM providers: Anthropic, Gemini, Ollama (all extend BaseLLMProvider)
├── tools/         # Execution tools: Shell, Read, Write, Fetch, Crypto, Process (all extend BaseTool)
├── core/          # ConfigManager (Pydantic), shell/process management
├── models/        # Pydantic models: Message, LLMResponse, ToolCall, TokenUsage
├── context/       # Prompt templates
├── agents/        # Agent implementations
└── utils/         # Utility functions
```

## Conventions

- **Async everywhere**: All provider `complete()` and tool `execute()` methods are async. Use `asyncio`.
- **Abstract base classes**: Providers extend `BaseLLMProvider`, tools extend `BaseTool`.
- **Pydantic 2.0+** for all data models, tool params (`{ToolName}Params`), and config.
- **Tool results**: All tools return `ToolResult(success, output, error, metadata)`.
- **Tool approval levels**: `NONE`, `OPTIONAL`, `REQUIRED` — respect per-tool settings.
- **Type hints required**: mypy strict mode is enabled. Use `Optional[T]` (not `T | None`) for Python 3.10 compat.
- **Formatting**: Black (line-length 88), isort (black profile).
- Known typo: `src/providers/anthrophic_provider.py` — do not rename without coordinating.

## Testing

- **Framework**: unittest (standard library) with `unittest.mock` for mocking
- **Test file naming**: `*_test.py` (e.g., `key_encoder_test.py`), NOT `test_*.py`
- **Test class naming**: `{Name}Test` (e.g., `StripAnsiTest`, `ChildAdapterTest`)
- **Location**: `tests/` mirrors `src/` structure (e.g., `tests/core/process/`)
- **Async tests**: Use `asyncio.run(coro)` (Python 3.13 removed implicit event-loop creation)
- **Mock helpers**: Define `_create_mock_*()` factory functions at module level
- **Run tests**: `python -m unittest tests/path/to_test.py -v`

## Common Commands
```bash
pip install -e ".[dev,test]"       # Install with dev dependencies
python -m src.cli.main             # Run the CLI
python -m unittest <test_name> -v  # Run a specific test
black src/ tests/                  # Format
isort src/ tests/                  # Sort imports
mypy src/                          # Type check
bandit -r src/                     # Security lint
```
