# Kerkov Bot

Security-focused conversational AI CLI for authorized penetration testing, security analysis, and CTF workflows.

## Prerequisites

- Python **3.13+**
- A supported LLM provider account (Gemini, OpenAI, Anthropic, or local Ollama)

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

- `exit`, `quit`, or `/q` — exit
- `clear` — clear conversation history
- `/mode` — show current mode
- `/mode [defense|audit|offensive|ctf]` — switch modes
- `/ctf-info` — view/update CTF context (CTF mode only)
- `/shell` — start a shared shell session
- `/model [name]` — switch models
- `/models` — list models for the current provider
- `/provider [name]` — switch providers
- `/providers` — list supported providers

## Configuration (.env)

The `.env` file lives at the repo root and is loaded at startup. Start from `.env.example` and set:

- `LLM_PROVIDER` (e.g., `gemini`, `openai`, `anthropic`, `ollama`)
- `DEFAULT_MODEL` (e.g., `gemini-2.5-flash`)
- Provider API keys (`GOOGLE_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`)
- Optional: `SANDBOX_MODE`, `MAX_CONTEXT_TOKENS`, `MAX_TIMEOUT_SECONDS`, `SESSION_DIR`

## Notes

- This tool is intended for **authorized** security testing only.
- See `docs/PROJECT_OVERVIEW.md` for a deeper architecture overview.
- See `docs/internal-dev/` for implementation details and internal docs.
