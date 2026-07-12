# Kerkov Bot

Security-focused conversational AI CLI for authorized penetration testing, security analysis, and CTF workflows.

## Prerequisites

- `uv`
- Python **3.10+** (Python 3.13 is pinned for local dev/CI)
- A supported LLM provider account (Gemini, NVIDIA NIM, Anthropic, Codex, or local Ollama)

## Quickstart

1) Install dependencies with the recommended uv workflow:

```bash
uv sync --locked --extra dev --extra test
```

Alternative editable install with pip:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev,test]"
kerkov --help
```

Use `uv sync --locked --extra dev --extra test` for reproducible dev and CI
setups. The pip workflow is useful for standard Python compatibility, but it
does not install from `uv.lock`.

2) Configure environment variables:

- Copy the example file in the repo root:

```bash
cp .env.example .env
```

- Edit `.env` and set your API keys and defaults (e.g., `GOOGLE_API_KEY`, `LLM_PROVIDER`, `DEFAULT_MODEL`).
- Copy `config/codex.example.json` to `config/codex.json` if you want to use Codex.

3) Start the CLI:

```bash
uv run kerkov
```

## Usage

Once running, type requests directly and use built-in commands:

- `exit`, `quit`, or `/q` - exit
- `clear` - clear conversation history
- `/mode` - show current mode
- `/mode [recon|audit|offensive|ctf]` - switch modes
- `/model [name]` - switch models
- `/models` - list models for the current provider
- `/provider [name]` - switch providers
- `/providers` - list supported providers
- `/codex-login` - open browser OAuth login for Codex and save runtime tokens into the Codex state file

## Development

Run commands inside the managed uv environment:

```bash
uv run python -m pytest tests
uv run python -m flake8 --select=E301,E302,E303,E305,E306 src tests
uv lock
```

Dependencies are declared in `pyproject.toml` and locked in `uv.lock`. When
changing dependencies, update `pyproject.toml`, run `uv lock`, then verify with
`uv sync --locked --extra dev --extra test`.

## Public Install

After release, install the CLI with either pip or uv:

```bash
python -m pip install kerkov-bot
kerkov
```

```bash
uv tool install kerkov-bot
kerkov
```

For one-off usage without keeping the tool installed:

```bash
uvx --from kerkov-bot kerkov
```

Before a PyPI release, install directly from GitHub:

```bash
uv tool install git+https://github.com/Kerkov-Security/kerkov-bot.git
kerkov
```

## Configuration

The `.env` file lives at the repo root and is loaded at startup. Start from `.env.example` and set:

- `LLM_PROVIDER` (e.g., `gemini`, `nvidia`, `anthropic`, `codex`, `ollama`)
- The provider-specific default model (e.g., `DEFAULT_NVIDIA_MODEL=z-ai/glm-5.2`)
- Provider API keys (`GOOGLE_API_KEY`, `NVIDIA_API_KEY`, `ANTHROPIC_API_KEY`)
- Codex static settings live in `config/codex.json`
- Runtime Codex auth state lives in the JSON state file configured by `CODEX_STATE_PATH` in `config/codex.json` (default: `data/providers/codex/auth.json`)
- Note: `/codex-login` defaults to Codex CLI values if unset (`client_id=app_EMoamEEZ73f0CkXaXp7hrann`, redirect `http://localhost:1455/auth/callback`)
- Optional: `SANDBOX_MODE`, `MAX_CONTEXT_TOKENS`, `MAX_TIMEOUT_SECONDS`, `SESSION_DIR`

## Notes

- This tool is intended for **authorized** security testing only.
- See `docs/PROJECT_OVERVIEW.md` for a deeper architecture overview.
- See `docs/internal-dev/` for implementation details and internal docs.
