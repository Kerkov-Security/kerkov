# Configuration

Every setting Kerkov reads, with its environment variable, default, and effect.
Settings come from these places, in order of precedence:

1. **The process environment** — any variable already exported in your shell.
2. **A `.env` file** — searched for in the working directory and up to three
   parent directories. Values here do **not** override variables already set in
   the environment.
3. **Saved user preferences** in `~/.kerkov/settings.json` (or
   `KERKOV_SETTINGS_FILE`) for supported settings, including the startup
   provider/model and Ollama endpoint. The web console's **Save as default**
   writes these preferences; it does not rewrite `.env`.
4. **Built-in defaults** — listed below.

Successful interactive CLI provider selections also save `LLM_PROVIDER` there;
credentials stay in their separate store. Remove existing `LLM_PROVIDER` and
`DEFAULT_*_MODEL` lines from `.env` to use saved choices instead of explicit
overrides.

Copy [`​.env.example`](https://github.com/Kerkov-Security/kerkov/blob/main/.env.example)
to `.env` and edit it, or export the variables directly. Booleans accept
`true`/`false` (case-insensitive); unset optional values fall back to the
defaults below.

!!! tip "You rarely need most of this"
    A working setup is one provider key and, optionally, `LLM_PROVIDER`.
    Everything else has a default. The tables below are the exhaustive
    reference, not a required checklist.

## Provider credentials and endpoints

API keys can also be entered at runtime with [`/auth`](slash-commands.md#auth)
or [Manage providers](../guides/web-console.md#providers-and-models) and stored
in your OS keyring or a private local file. A key entered for the current runtime
session overrides environment and stored credentials. In the web console, these
overrides belong to one browser connection and are discarded on disconnect;
only saved keys are shared. New sessions prefer environment keys over stored keys.

| Variable | Default | Effect |
|---|---|---|
| `ANTHROPIC_API_KEY` | _(unset)_ | API key for the `anthropic` provider. |
| `GOOGLE_API_KEY` | _(unset)_ | API key for the `gemini` provider. |
| `NVIDIA_API_KEY` | _(unset)_ | API key for the `nvidia` (NIM) provider. |
| `NVIDIA_BASE_URL` | `https://integrate.api.nvidia.com/v1` | NVIDIA NIM API endpoint. |
| `OPENROUTER_API_KEY` | _(unset)_ | API key for the `openrouter` provider. |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | OpenRouter API endpoint. |
| `OPENROUTER_HTTP_REFERER` | _(unset)_ | Optional `HTTP-Referer` header OpenRouter uses for app attribution. |
| `OPENROUTER_APP_TITLE` | _(unset)_ | Optional `X-Title` header for OpenRouter app attribution. |
| `DEEPSEEK_API_KEY` | _(unset)_ | API key for the `deepseek` provider. |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | DeepSeek API endpoint. |
| `OPENAI_API_KEY` | _(unset)_ | OpenAI key, recognized as a stored credential. |
| `OLLAMA_BASE` | `http://localhost:11434` | Base URL of your local Ollama server. |

### Codex OAuth

Codex authenticates over OAuth; sign-in starts automatically when you select the
`codex` provider, and runtime tokens are written to `CODEX_STATE_PATH`. Static
OAuth client configuration normally lives in `config/codex.json` (see
[`config/codex.example.json`](https://github.com/Kerkov-Security/kerkov/blob/main/config/codex.example.json));
these variables override it.

| Variable | Default | Effect |
|---|---|---|
| `CODEX_ACCESS_TOKEN` | _(unset)_ | Pre-supplied access token; skips interactive sign-in. |
| `CODEX_OAUTH_CLIENT_ID` | _(unset)_ | OAuth client ID. |
| `CODEX_OAUTH_CLIENT_SECRET` | _(unset)_ | OAuth client secret. |
| `CODEX_OAUTH_REFRESH_TOKEN` | _(unset)_ | Refresh token for renewing access. |
| `CODEX_OAUTH_TOKEN_URL` | _(unset)_ | Token endpoint. |
| `CODEX_OAUTH_AUTHORIZE_URL` | _(unset)_ | Authorization endpoint. |
| `CODEX_OAUTH_REDIRECT_URI` | `http://localhost:1455/auth/callback` | Local redirect URI for the sign-in callback. |
| `CODEX_OAUTH_CALLBACK_BIND_HOST` | _(unset)_ | Host the callback server binds to (defaults to the redirect URI's host). |
| `CODEX_OAUTH_SCOPE` | `openid profile email offline_access` | Requested OAuth scopes. |
| `CODEX_BASE_URL` | `https://chatgpt.com/backend-api` | Codex API endpoint. |
| `CODEX_STATE_PATH` | `data/providers/codex/auth.json` | Where runtime OAuth tokens are persisted. |
| `CODEX_ACCOUNT_ID` | _(unset)_ | Account ID sent with Codex requests. |
| `CODEX_TOKEN_EXPIRES_AT` | _(unset)_ | Cached token expiry timestamp. |

## Provider and model selection

| Variable | Default | Effect |
|---|---|---|
| `LLM_PROVIDER` | `gemini` | Startup provider fallback; environment and `.env` values override the saved choice. One of `anthropic`, `gemini`, `nvidia`, `openrouter`, `deepseek`, `ollama`, `codex`. |
| `DEFAULT_ANTHROPIC_MODEL` | `claude-sonnet-4-5-20250929` | Model used when the provider is `anthropic`. |
| `DEFAULT_GEMINI_MODEL` | `gemini-2.5-flash` | Model used when the provider is `gemini`. |
| `DEFAULT_NVIDIA_MODEL` | `z-ai/glm-5.2` | Model used when the provider is `nvidia`. |
| `DEFAULT_OPENROUTER_MODEL` | `openrouter/auto` | Model used when the provider is `openrouter`. |
| `DEFAULT_DEEPSEEK_MODEL` | `deepseek-flash` | Model used when the provider is `deepseek`. |
| `DEFAULT_OLLAMA_MODEL` | `gpt-oss` | Model used when the provider is `ollama`. |
| `DEFAULT_CODEX_MODEL` | `gpt-5.4` | Model used when the provider is `codex`. |

Switch either at runtime with [`/provider`](slash-commands.md#providers-and-models)
and [`/model`](slash-commands.md#providers-and-models).

## Interface selection

| Setting | Default | Effect |
|---|---|---|
| `INTERFACE` | _(prompt)_ | Saved startup interface, `cli` or `web`. Lives only in `~/.kerkov/settings.json`, not `.env`. With no saved value and an interactive terminal, `kerkov` asks on first launch and offers to remember the choice. |

The [`--interface`](cli.md#kerkov) flag overrides the saved value for one run;
`--message`, `--continue`, and `--resume` always use the CLI. Picking `web`
launches the console (`kerkov-web`), which needs the `web` extra.

## Context and compaction

| Variable | Default | Effect |
|---|---|---|
| `MAX_CONTEXT_TOKENS` | `50000` | Context budget; compaction triggers at a fraction of this. |
| `MAX_RESPONSE_TOKENS` | _(unset)_ | Cap on tokens per model response; unset uses the provider default. |
| `COMPACTION_THRESHOLD_PCT` | `0.65` | Fraction of `MAX_CONTEXT_TOKENS` at which compaction runs. |
| `SUMMARY_RESERVE_TOKENS` | `2000` | Tokens reserved for the compaction summary. |
| `TOOL_OUTPUT_MAX_CHARS` | `6000` | Tool output above this is normalized (collapsed, then head/tail trimmed) for history. |
| `TOOL_OUTPUT_HEAD_CHARS` | `1500` | Characters kept from the start of over-long tool output. |
| `TOOL_OUTPUT_TAIL_CHARS` | `500` | Characters kept from the end of over-long tool output. |

## Tool execution

| Variable | Default | Effect |
|---|---|---|
| `EXECUTION_ENABLED` | `true` | Master switch for local execution: `exec`, `container_lifecycle`, `sbom_generate`, `vulnerability_scan`, `sast_scan`, and `zap`'s managed-container startup. `process` is not gated — it drives sessions `exec` already started. |
| `WRITE_TOOL_ENABLED` | `true` | Master switch for the `write` tool. |
| `REQUIRE_SUDO_APPROVAL` | `true` | Reserved; declared but not read by the runtime. The packaged `deny` rule on `sudo` in `approvals.yaml` is what enforces the boundary. |
| `MAX_TIMEOUT_SECONDS` | `60` | Request timeout for provider API calls, and the `http` tool's fallback timeout. Not a command timeout: `exec` carries its own `timeout` (default `120`). |
| `MAX_RATE_LIMIT` | `10` | Reserved; not currently read by the runtime. |
| `HTTP_PROFILES_PATH` | `config/http_profiles.yaml` | Named-credential profiles for the `http` tool. |

## Autonomy, verbosity, and approvals

Autonomy is a preset over two independent axes — narration verbosity and
approval gating — either of which can be pinned on its own. See the
[modes and autonomy guide](../guides/modes-and-autonomy.md).

| Variable | Default | Effect |
|---|---|---|
| `AUTONOMY_LEVEL` | `2` | Preset: `1` guided, `2` standard, `3` aggressive, `4` autonomous. |
| `ALLOW_AUTONOMOUS` | `false` | Must be `true` to allow autonomy level 4. |
| `VERBOSITY` | _(unset)_ | Pin verbosity (`guided`/`standard`/`aggressive`/`silent`), overriding the autonomy preset. |
| `STREAM_OUTPUT` | `true` | Stream the assistant's reply as raw text in the CLI and web console. Set `false` to wait for the completed reply; the CLI then renders it as Markdown. Delegated agents do not stream. |
| `APPROVAL_MODE` | _(unset)_ | Pin approval gating (`strict`/`normal`/`permissive`/`yolo`), overriding the autonomy preset. |
| `USER_APPROVALS_PATH` | `~/.kerkov/approvals.yaml` | Per-user overrides for the packaged approval rules. |

## Agents

| Variable | Default | Effect |
|---|---|---|
| `AGENT_MAX_DEPTH` | `2` | Deepest a delegation chain may nest. |
| `AGENT_MAX_TOTAL` | `20` | Total agents allowed per session. |
| `AGENT_MAX_BATCH` | `5` | Agents one `delegate` call may spawn at once. |
| `AGENT_MAX_CONCURRENT` | `5` | Live agents allowed at the same time. |
| `AGENT_MAX_ITERATIONS` | `30` | Tool-call iterations an agent may run before stopping. |
| `AGENT_TEMPERATURE` | _(unset)_ | Sampling temperature for agents; unset uses the provider default. |
| `AGENT_ROUTING_ENABLED` | `false` | Opt in to model routing for delegated agents using the active provider. Invalid routing settings fall back to manual model selection. |
| `AGENT_ROUTING_SMALL_MODEL` | _(empty)_ | Model ID for eligible bounded extraction and formatting tasks using the built-in analysis profile with only `read` and `recall` tools. |
| `AGENT_ROUTING_MEDIUM_MODEL` | _(empty)_ | Model ID for eligible bounded analysis tasks with the same profile and tool restrictions. |
| `AGENT_ROUTING_LARGE_MODEL` | _(empty)_ | Fallback model ID for other delegated tasks, including security decisions and actions. |

Routing requires three distinct, nonempty model IDs without whitespace. It does
not change the main conversation's model or the agent's tool permissions.

## Usage estimates

| Variable | Default | Effect |
|---|---|---|
| `MODEL_PRICING_FILE` | _(unset)_ | Local JSON pricing file used by `/usage` to estimate costs from saved token counts. No rates are bundled; unavailable rates or token counts remain unknown. |

Use version `1` with rates in USD per million tokens, keyed by `provider/model`.
For example, these illustrative rates are not a provider price quote:

```json
{"version": 1, "models": {"ollama/example-model": {"input": 1, "output": 2, "cache_read": 0.1, "cache_write": 1.25}}}
```

`input` and `output` are required. A missing cache rate leaves a call's cost
unknown when that call uses the corresponding cache operation. `/usage` includes
delegated agents and can inspect a saved engagement by ID.

## Knowledge base and findings ledger

Both `recall` and `ledger` share the same master switch.

| Variable | Default | Effect |
|---|---|---|
| `KB_ENABLED` | `true` | Enables tool-output ingestion and the `recall` and `ledger` tools. |
| `KB_CLIP_THRESHOLD_BYTES` | `8000` | Output-plus-metadata footprint above which a result gets an `artifact#N` reference. |
| `KB_EMBEDDINGS` | `false` | Opt-in semantic recall; default is offline FTS5 search only. |
| `KB_EMBEDDING_PROVIDER` | _(unset)_ | Embedding backend name; inert until a backend ships. |

## Web search (`web_search`)

| Variable | Default | Effect |
|---|---|---|
| `SEARCH_PROVIDER` | `tavily` | Search backend: `tavily` (API key) or `searxng` (self-hosted). |
| `SEARCH_API_KEY` | _(unset)_ | Tavily API key. |
| `SEARCH_BASE_URL` | _(unset)_ | SearXNG instance root, e.g. `http://127.0.0.1:8080`. Required for `searxng`. |
| `SEARCH_MAX_RESULTS` | `10` | Maximum results returned per search. |
| `SEARCH_TIMEOUT_SECONDS` | `15` | Per-search request timeout. |

### Managed SearXNG (`/searxng`)

Used by the [`/searxng`](slash-commands.md#tools) command, which provisions a
local instance and sets `SEARCH_PROVIDER`/`SEARCH_BASE_URL` for you.

| Variable | Default | Effect |
|---|---|---|
| `SEARXNG_DOCKER_IMAGE` | `docker.io/searxng/searxng:latest` | Image the managed instance runs. |
| `SEARXNG_DOCKER_CONTAINER_NAME` | `kerkov-searxng` | Container name for the managed instance. |
| `SEARXNG_CONFIG_DIR` | `~/.kerkov/searxng` | Where the generated `settings.yml` is written. |
| `SEARXNG_STARTUP_TIMEOUT_SECONDS` | `120` | How long to wait for the instance to answer. |

## Web page reads (`read_url`)

| Variable | Default | Effect |
|---|---|---|
| `READ_MAX_CHARS` | `20000` | Output budget for a page read, truncation markers included. |
| `READ_TIMEOUT_SECONDS` | `15` | Per-fetch timeout. |

## CVE intelligence (`cve_lookup`)

All CVE sources are keyless; the NVD key only raises a rate limit.

| Variable | Default | Effect |
|---|---|---|
| `NVD_API_KEY` | _(unset)_ | Optional NVD key; raises NVD's rate limit. |
| `CVE_CACHE_TTL_HOURS` | `24` | How long merged CVE records stay cached. |
| `INTEL_CACHE_DIR` | `~/.kerkov/cache/intel` | Disk cache for CVE intelligence. |
| `CVE_LOOKUP_TIMEOUT_SECONDS` | `15` | Per-source request timeout. |

## OWASP ZAP (`zap`, `/zap`)

| Variable | Default | Effect |
|---|---|---|
| `ZAP_API_URL` | `http://127.0.0.1:8090` | ZAP daemon API endpoint. |
| `ZAP_API_TIMEOUT_SECONDS` | `15` | Per-request timeout for the ZAP API. |
| `ZAP_API_VERIFY_TLS` | `true` | Verify TLS when the ZAP API is HTTPS. |
| `ZAP_API_ALLOW_REMOTE` | `false` | Permit a non-loopback ZAP endpoint. |
| `ZAP_MANAGED` | `false` | Whether a *configured* ZAP endpoint that stops answering may be replaced by a managed container. Not required for the managed runtime itself: the first scan with no stored ZAP key starts one regardless. |
| `ZAP_STARTUP_TIMEOUT_SECONDS` | `180` | How long to wait for a managed ZAP to be ready. |
| `ZAP_DOCKER_IMAGE` | `ghcr.io/zaproxy/zaproxy:stable` | Image the managed ZAP runs. |
| `ZAP_DOCKER_CONTAINER_NAME` | `kerkov-zap` | Container name for the managed ZAP. |

## Storage and logging

| Variable | Default | Effect |
|---|---|---|
| `WORKSPACE_DIR` | `~/.kerkov/workspaces` | Root for per-engagement working directories. |
| `LOG_LEVEL` | `INFO` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |
| `AUDIT_LOG_PATH` | `data/logs/audit.log` | Reserved; not currently read by the runtime. |
| `SESSION_DIR` | `data/sessions` | Reserved; not currently read by the runtime. |
| `AUTO_SAVE_INTERVAL` | `300` | Reserved; not currently read by the runtime. |

!!! note "Engagement transcripts"
    Conversations are stored as JSONL under `~/.kerkov/engagements/`, and each
    engagement tree gets a SQLite knowledge base at
    `~/.kerkov/engagements/<root-id>.kb.sqlite`. These paths are fixed, not
    configured through the variables above.

## Settings read outside the main loader

These are read directly by their subsystems, not by the central configuration
loader, so they are documented here for completeness.

### Credential storage

| Variable | Default | Effect |
|---|---|---|
| `KERKOV_CREDENTIAL_STORE` | _(auto)_ | Backend for stored secrets: `keyring`, `file`, or `memory`. |
| `KERKOV_CREDENTIAL_FILE` | `~/.kerkov/credentials.json` | Path used by the `file` credential backend. |

### Web console (`kerkov-web`)

| Variable | Default | Effect |
|---|---|---|
| `KERKOV_WEB_SECRET_KEY` | _(generated)_ | Django secret key; a fresh one is generated per run if unset. |
| `KERKOV_WEB_ACCESS_TOKEN` | _(generated)_ | Access token guarding the console; printed at startup if generated. |
| `KERKOV_WEB_ALLOWED_HOSTS` | `127.0.0.1,localhost` | Allowed hosts; only loopback hosts are accepted. |
| `KERKOV_WEB_DEBUG` | `0` | Django debug mode; any value other than `0`/`false`/`no` enables it. |
