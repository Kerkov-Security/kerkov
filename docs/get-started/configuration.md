# Configuration

Kerkov works out of the box with a single provider credential. This page covers
how credentials and settings are resolved so you can go beyond the defaults; the
[configuration reference](../reference/configuration.md) is the exhaustive list
of every variable.

## Credentials come first

The only thing Kerkov needs to start is access to one model provider. You never
have to put a key in a file:

- On first launch, pick a provider and paste the key at the **masked prompt**.
  Kerkov validates it, then stores it in your OS keyring (or a user-only file
  under `~/.kerkov/` on headless systems).
- Manage keys later with [`/auth`](../reference/slash-commands.md#auth) — add,
  replace, or remove them without the value ever entering the conversation.

Because keys are entered this way, they stay out of your chat history and logs.
Environment variables remain available for CI and other non-interactive use
(see below).

Your provider choice is remembered too. Once a provider is successfully selected
at startup or with [`/provider`](../reference/slash-commands.md#providers-and-models),
Kerkov saves it in `~/.kerkov/settings.json` and uses it the next time you open
Kerkov. No `.env` file is needed. Credentials and Codex sign-in tokens are stored
separately from this non-secret preference.

## Where settings come from

Settings are read in this order of precedence:

1. **The process environment** — anything already exported in your shell.
2. **A `.env` file** — searched for in the working directory and up to three
   parent directories.
3. **Per-user settings** — supported interactive preferences saved in
   `~/.kerkov/settings.json` (`KERKOV_SETTINGS_FILE` overrides its location).
4. **Built-in defaults.**

A variable set in the environment wins over the same variable in `.env`. Both
override saved user preferences: remove an explicit `LLM_PROVIDER` override if
you want startup to follow your remembered provider choice. For overrides, copy
the template and edit only what you need:

```bash
cp .env.example .env
```

No `.env` file is required for interactive use; it exists for overrides and for
environments where the interactive credential prompt is not available.

## The `config/` directory

Two files under `config/` carry structured configuration that does not fit an
environment variable:

- **`config/http_profiles.yaml`** — named credential profiles for the `http`
  tool, so requests can authenticate without secrets in the prompt. Point
  elsewhere with `HTTP_PROFILES_PATH`.
- **`config/codex.json`** — static OAuth client configuration for the Codex
  provider (untracked; copy from `config/codex.example.json`). Runtime tokens
  are written separately, to the path in `CODEX_STATE_PATH`.

## Approval rules

Which tool calls run without asking is governed by approval rules, not a single
setting. Packaged defaults load automatically; your overrides live at
`~/.kerkov/approvals.yaml` (`USER_APPROVALS_PATH`). See the
[approvals guide](../guides/approvals.md) for the rule syntax and how modes
change evaluation.

## Where your data lives

| Data | Location |
|---|---|
| Engagement transcripts | `~/.kerkov/engagements/<id>.jsonl` |
| Knowledge base (per engagement tree) | `~/.kerkov/engagements/<root-id>.kb.sqlite` |
| Working directories | `~/.kerkov/workspaces/` (`WORKSPACE_DIR`) |
| Stored credentials (file backend) | `~/.kerkov/credentials.json` |
| Saved interactive preferences | `~/.kerkov/settings.json` |
| User approval overrides | `~/.kerkov/approvals.yaml` |

Everything Kerkov stores is local and unencrypted, harvested credentials
included. Nothing is sent anywhere but your chosen model provider and the tools
you invoke.

## Common first settings

| Want to… | Set |
|---|---|
| Override the saved startup provider | `LLM_PROVIDER` |
| Use a different default model | `DEFAULT_<PROVIDER>_MODEL` |
| Allow longer conversations | `MAX_CONTEXT_TOKENS` (default 50000) |
| Allow fully autonomous runs | `ALLOW_AUTONOMOUS=true` |

The [configuration reference](../reference/configuration.md) lists all 89
settings with their defaults.
