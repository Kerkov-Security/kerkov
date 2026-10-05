# Providers and Models

Kerkov works with the model you prefer. Seven providers are supported, and you
switch between them — and between models within a provider — at runtime without
changing anything else about the workflow.

| Provider | Aliases | Authentication |
|---|---|---|
| `anthropic` | `claude` | API key |
| `gemini` | `google`, `google-gemini` | API key |
| `nvidia` | `nim`, `nvidia-nim` | API key (NVIDIA NIM) |
| `openrouter` | `open-router` | API key |
| `deepseek` | — | API key |
| `ollama` | — | Local server URL; no key by default |
| `codex` | `openai-codex` | Browser-based OAuth sign-in |

## Choosing a provider

Set the startup provider with `LLM_PROVIDER` (default `gemini`), or switch in a
session:

```text
kerkov › /provider
kerkov › /provider claude
```

With no argument, [`/provider`](../reference/slash-commands.md#providers-and-models)
opens a picker and highlights the active one. Selecting **codex** starts browser
sign-in when it's needed; the resulting tokens are stored under
`CODEX_STATE_PATH` and refreshed automatically.

## Choosing a model

Each provider has a default model (see the
[configuration reference](../reference/configuration.md#provider-and-model-selection)).
Change it with [`/model`](../reference/slash-commands.md#providers-and-models),
or set a `DEFAULT_<PROVIDER>_MODEL` variable:

```text
kerkov › /model
kerkov › /model claude-sonnet-4-5-20250929
```

The picker lists what the current provider offers; switching provider resets the
model to that provider's default unless you pick another.

## Credentials

API keys are entered at a **masked prompt** — with [`/auth`](../reference/slash-commands.md#auth),
or automatically the first time you select a provider that needs one — so they
never enter the conversation or logs. Kerkov validates a key before saving it to
your OS keyring (or a user-only file on headless systems).

Environment variables remain available for CI and non-interactive use: set
`ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `NVIDIA_API_KEY`, `OPENROUTER_API_KEY`,
`DEEPSEEK_API_KEY`, and the rest as listed in the
[configuration reference](../reference/configuration.md#provider-credentials-and-endpoints).

Remove a stored credential with [`/logout`](../reference/slash-commands.md#auth).

In the [web console](web-console.md#providers-and-models), **Manage providers**
offers API-key entry, connection testing, and Ollama endpoint settings. Use
**Save as default** in the model picker to persist a startup provider/model.
Codex sign-in still starts through the CLI.

## Provider notes

- **Ollama** talks to a local server at `OLLAMA_BASE`
  (`http://localhost:11434` by default) and needs no key. Point it at a remote
  Ollama by changing that URL.
- **NVIDIA NIM**, **OpenRouter**, and **DeepSeek** each have a `*_BASE_URL`
  override for self-hosted or proxied endpoints. OpenRouter also accepts
  `OPENROUTER_HTTP_REFERER` and `OPENROUTER_APP_TITLE` for app attribution.
- **Codex** uses OAuth rather than a key; static client configuration lives in
  `config/codex.json` (copy from `config/codex.example.json`).

## Context budget

Conversation length is bounded by `MAX_CONTEXT_TOKENS` (default 50000) and
per-response output by `MAX_RESPONSE_TOKENS`; compaction runs at
`COMPACTION_THRESHOLD_PCT` (65%) of the budget. Raise these for longer
conversations, within the selected model's limits and your cost budget. What
survives compaction is covered under [Memory](memory.md).
