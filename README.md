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

4) Start the CLI:

```bash
python -m src.cli.main
```

## Usage

Once running, type requests directly and use built-in commands:

- `exit`, `quit`, or `/q` - exit
- `clear` - clear conversation history
- `/mode` - show current mode
- `/mode [defense|audit|offensive|ctf]` - switch modes
- `/ctf-info` - view/update CTF context (CTF mode only)
- `/shell` - start a shared shell session
- `/model [name]` - switch models
- `/models` - list models for the current provider
- `/provider [name]` - switch providers
- `/providers` - list supported providers
- `/codex-login` - open browser OAuth login for Codex and save tokens into `.env`

## Configuration (.env)

The `.env` file lives at the repo root and is loaded at startup. Start from `.env.example` and set:

- `LLM_PROVIDER` (e.g., `gemini`, `anthropic`, `codex`, `ollama`)
- `DEFAULT_MODEL` (e.g., `gemini-2.5-flash`)
- Provider API keys/tokens (`GOOGLE_API_KEY`, `ANTHROPIC_API_KEY`, `CODEX_ACCESS_TOKEN`)
- Optional Codex OAuth settings (`CODEX_OAUTH_CLIENT_ID`, `CODEX_OAUTH_CLIENT_SECRET`, `CODEX_OAUTH_REFRESH_TOKEN`, `CODEX_OAUTH_TOKEN_URL`, `CODEX_OAUTH_AUTHORIZE_URL`, `CODEX_OAUTH_REDIRECT_URI`, `CODEX_OAUTH_SCOPE`, `CODEX_BASE_URL`, `DEFAULT_CODEX_MODEL`, `CODEX_ACCOUNT_ID`, `CODEX_TOKEN_EXPIRES_AT`)
- Note: `/codex-login` defaults to Codex CLI values if unset (`client_id=app_EMoamEEZ73f0CkXaXp7hrann`, redirect `http://localhost:1455/auth/callback`)
- Optional: `SANDBOX_MODE`, `MAX_CONTEXT_TOKENS`, `MAX_TIMEOUT_SECONDS`, `SESSION_DIR`

## Notes

- This tool is intended for **authorized** security testing only.
- See `docs/PROJECT_OVERVIEW.md` for a deeper architecture overview.
- See `docs/internal-dev/` for implementation details and internal docs.
