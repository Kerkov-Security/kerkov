# Kerkov Bot

Security-focused conversational AI CLI for authorized penetration testing, security analysis, and CTF workflows.

## Prerequisites

- `uv`
- Python **3.10+** (Python 3.13 is pinned for local dev/CI)
- A supported LLM provider account (Gemini, NVIDIA NIM, OpenRouter, Anthropic, Codex, or local Ollama)

## Quickstart

1) Install dependencies with the recommended uv workflow:

```bash
uv sync --locked --extra dev --extra test --extra web
```

Alternative editable install with pip:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev,test,web]"
kerkov --help
```

Use `uv sync --locked --extra dev --extra test --extra web` for reproducible
development and CI setups. CLI-only users can omit `--extra web`. The pip
workflow is useful for standard Python compatibility, but it does not install
from `uv.lock`.

2) Configure non-secret defaults:

- Copy the example file in the repo root:

```bash
cp .env.example .env
```

- Edit `.env` only for ordinary settings such as `LLM_PROVIDER` and default
  models. API keys do not need to be placed in this file.
- Copy `config/codex.example.json` to `config/codex.json` if you want to use Codex.

3) Start the CLI:

```bash
uv run kerkov
```

When the selected provider needs an API key, Kerkov opens a separate masked
terminal prompt. The key bypasses chat history and logs and is saved in the
operating-system keyring after it is validated. On headless or SSH systems
without a keyring, Kerkov falls back to a user-only credential file at
`~/.kerkov/credentials.json`. Use `--session` with `/auth` when you do not want
a key persisted.

4) Start the experimental web console:

```bash
uv run --no-sync kerkov-web runserver
```

Open `http://127.0.0.1:8764/`. The web console currently proves the
Django + Channels path with a real Kerkov runtime adapter: browser messages are
sent through the shared conversation engine, tool activity streams back over
WebSockets, and tool calls that require a decision pause for browser approval.
The experimental web console can use credentials already saved through the CLI
or supplied by the process environment. It does not yet expose its own
credential-entry prompt.
The prototype has no user authentication and intentionally supports loopback
hosts only. Do not bind it to a LAN address or expose it through a reverse
proxy. Web-specific settings are also loaded from `.env`; their safe defaults
normally require no changes:

- `KERKOV_WEB_DEBUG=0`
- `KERKOV_WEB_ALLOWED_HOSTS=127.0.0.1,localhost,[::1]`
- `KERKOV_WEB_SECRET_KEY` and `KERKOV_WEB_ACCESS_TOKEN` default to fresh random
  values for each server process.

## Usage

Once running, type requests directly and use built-in commands:

- `exit`, `quit`, or `/q` - exit
- `clear` - clear conversation history
- `/mode` - show current mode
- `/mode [recon|audit|offensive|ctf]` - switch modes
- `/model [name]` - open the model picker or switch directly by name
- `/models [name]` - compatibility alias for `/model`
- `/provider [name]` - choose from the interactive provider picker or switch directly
- `/providers` - compatibility alias for `/provider`
- `/auth` - show credential status without displaying secret values
- `/auth set [provider|tavily] [--session]` - enter a key in a masked prompt
- `/auth remove [provider|tavily]` - remove a stored key
- `/logout [provider|tavily|codex]` - choose and confirm a local credential to remove
- `/codex-login` - open browser OAuth login for Codex and save runtime tokens into the Codex state file

## Development

Run commands inside the managed uv environment:

```bash
uv run python -m pytest tests
uv run python -m pytest tests/web/django_web_test.py
uv run python -m flake8 --select=E301,E302,E303,E305,E306 src tests
uv lock
```

Dependencies are declared in `pyproject.toml` and locked in `uv.lock`. When
changing dependencies, update `pyproject.toml`, run `uv lock`, then verify with
`uv sync --locked --extra dev --extra test --extra web`.

## Public Install

After release, install the CLI with either pip or uv:

```bash
python -m pip install kerkov-bot
kerkov
```

Install the optional local web console with:

```bash
python -m pip install "kerkov-bot[web]"
kerkov-web runserver
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

Kerkov works without a `.env` file. Non-secret deployment settings can still be
supplied through the process environment or an optional repo-root `.env`; see
`.env.example` for the available compatibility overrides:

- `LLM_PROVIDER` (e.g., `gemini`, `nvidia`, `openrouter`, `anthropic`, `codex`, `ollama`)
- The provider-specific default model (e.g., `DEFAULT_NVIDIA_MODEL=z-ai/glm-5.2`, `DEFAULT_OPENROUTER_MODEL=openrouter/auto`)
- Provider and Tavily API keys are entered interactively and saved in the OS
  keyring when available. The default `KERKOV_CREDENTIAL_STORE=auto` falls back
  to `~/.kerkov/credentials.json` with directory mode `0700` and file mode
  `0600` on headless systems. The fallback relies on filesystem permissions and
  is not encrypted at rest. Set the store to `keyring` to forbid that fallback,
  or use `--session` for memory-only storage.
- Process environment variables remain supported for CI and other
  non-interactive deployments.
- Selecting Ollama prompts for its API server URL, defaulting to
  `http://localhost:11434`. The validated URL is saved as non-secret user
  configuration in `~/.kerkov/settings.json`; `OLLAMA_BASE` remains available
  as an environment override.
- Codex static settings live in `config/codex.json`
- Runtime Codex auth state lives in the JSON state file configured by `CODEX_STATE_PATH` in `config/codex.json` (default: `data/providers/codex/auth.json`)
- Note: `/codex-login` defaults to Codex CLI values if unset (`client_id=app_EMoamEEZ73f0CkXaXp7hrann`, redirect `http://localhost:1455/auth/callback`)
- Optional: `SANDBOX_MODE`, `MAX_CONTEXT_TOKENS`, `MAX_TIMEOUT_SECONDS`, `SESSION_DIR`

The repo-local `data/` directory is for runtime artifacts such as sessions,
workspaces, logs, and provider-managed state. Do not use it as a general secret
store: it does not provide encryption or OS-level access controls.

## Notes

- This tool is intended for **authorized** security testing only.
- See `docs/PROJECT_OVERVIEW.md` for a deeper architecture overview.
- See `docs/internal-dev/` for implementation details and internal docs.
- See `docs/internal-dev/WEB_ARCHITECTURE.md` for the current Django web UI
  boundary and event contract.
