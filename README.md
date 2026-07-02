# Kerkov Bot

Security-focused conversational AI CLI for authorized penetration testing, security analysis, and CTF workflows.

## Prerequisites

- Python **3.13+**
- A supported LLM provider account (Gemini, Anthropic, Codex, or local Ollama)

## Quickstart

1) Create a virtual environment (recommended):

```bash
python3.13 -m venv .venv
source .venv/bin/activate
```

2) Install dependencies:

```bash
pip install -r requirements.txt
```

3) Configure environment variables:

- Copy the example file in the repo root:

```bash
cp .env.example .env
```

- Edit `.env` and set your API keys and defaults (e.g., `GOOGLE_API_KEY`, `LLM_PROVIDER`, `DEFAULT_MODEL`).
- Copy `config/codex.example.json` to `config/codex.json` if you want to use Codex.

4) Start the CLI:

```bash
python -m src.cli.main
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

## Configuration

The `.env` file lives at the repo root and is loaded at startup. Start from `.env.example` and set:

- `LLM_PROVIDER` (e.g., `gemini`, `anthropic`, `codex`, `ollama`)
- `DEFAULT_MODEL` (e.g., `gemini-2.5-flash`)
- Provider API keys (`GOOGLE_API_KEY`, `ANTHROPIC_API_KEY`)
- Codex static settings live in `config/codex.json`
- Runtime Codex auth state lives in the JSON state file configured by `CODEX_STATE_PATH` in `config/codex.json` (default: `data/providers/codex/auth.json`)
- Note: `/codex-login` defaults to Codex CLI values if unset (`client_id=app_EMoamEEZ73f0CkXaXp7hrann`, redirect `http://localhost:1455/auth/callback`)
- Optional: `SANDBOX_MODE`, `MAX_CONTEXT_TOKENS`, `MAX_TIMEOUT_SECONDS`, `SESSION_DIR`

## Notes

- This tool is intended for **authorized** security testing only.
- See `docs/PROJECT_OVERVIEW.md` for a deeper architecture overview.
- See `docs/internal-dev/` for implementation details and internal docs.
