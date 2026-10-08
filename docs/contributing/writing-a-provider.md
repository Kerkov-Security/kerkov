# Writing a provider

A provider adapts one LLM backend to the single interface the
[conversation engine](../concepts/conversation-loop.md) drives, so the engine
never knows which model it is talking to. Kerkov ships seven; adding an eighth is
a base class, a factory branch, a credential spec, and config fields.

## 1. Subclass `BaseLLMProvider`

`BaseLLMProvider` (`src/kerkov/providers/base_provider.py`) is constructed with
`(api_key, model)` and declares five abstract methods you must implement:

| Method | Signature | Returns |
|---|---|---|
| `complete` | `async (messages, temperature, max_tokens, tools, on_delta)` | `LLMResponse` |
| `validate_connection` | `()` | `bool` |
| `list_models` | `()` | `List[str]` |
| `get_max_context_tokens` | `async ()` | `Optional[int]` |
| `get_max_output_tokens` | `async ()` | `Optional[int]` |

`complete` is the heart of it: translate `messages` and `tools` into the
backend's request shape, call the API, and return a normalized `LLMResponse`
(text plus any tool calls). The `on_delta` callback is the streaming seam — when
it is given, invoke it with each chunk of assistant text as it arrives; when it
is `None`, accumulate and return the whole response. **The returned
`LLMResponse` must be identical either way**; agents take the non-streaming path, as do the CLI and web session when
`STREAM_OUTPUT=false`. Both front ends stream by default, so keep behavior
consistent across streaming settings and agent runs.

The two `get_max_*_tokens` coroutines back [context compaction](../concepts/context-and-compaction.md).
Cache per-model results and keep any HTTP or SDK lookup off the event loop; both
may return `None` if the provider genuinely cannot determine the value.

### Override `caches_prompt_prefix` if the backend caches a prefix

`caches_prompt_prefix` is a property that defaults to `False`. Return `True` when
the backend caches a stable prompt prefix across turns (Anthropic and DeepSeek
do). The engine reads it as a *placement hint*: when it is true, the mutable
[findings ledger](../concepts/findings-ledger.md) is injected at the **tail** of
the request so a ledger update never invalidates the cached
system-plus-tools-plus-history prefix; when false, the ledger is placed high,
where it is most salient. It is a hint about placement, not a cache
configuration — the provider relies on the API's automatic prefix caching.

## 2. Register it in the factory

Two edits in `src/kerkov/providers/__init__.py`:

First, add the canonical name to `SUPPORTED_PROVIDERS`. Then add a branch to
`_create_provider` (reached by both `create_initial_provider` and
`create_provider_from_name`) that matches the name and its aliases, resolves the
key, and reads the model from config:

```python
    if provider_name in ("acme", "acme-ai"):
        api_key = _resolve_api_key(config.acme_api_key, provider_name, credentials)

        from kerkov.providers.acme_provider import AcmeProvider

        return AcmeProvider(
            api_key=api_key,
            base_url=config.acme_base_url,
            model=config.default_acme_model,
        )
```

`_resolve_api_key` looks the key up through the `CredentialManager` first
(keyring or local file), then falls back to the config value, and raises
`MissingCredentialError` if neither is set — which is what lets the CLI prompt
for a missing key at runtime. A keyless backend (like Ollama) skips this and
reads a base URL from config instead.

`ProviderManager` (`src/kerkov/providers/provider_manager.py`) owns runtime
switching, but it delegates to `create_provider_from_name`, so once your branch
is in the factory, `/provider` and `/model` switch to it with no further change.

## 3. Give it a credential spec

For a keyed provider, `_resolve_api_key` resolves its spec through
`provider_credential_spec` (`src/kerkov/core/credentials.py`). Add a
`CredentialSpec` constant there and register it in the provider-credential map
(with any aliases in the alias map), so the manager knows which key to store and
which environment variable backs it. Without this, `_resolve_api_key` raises
"has no credential specification".

## 4. Add the config fields and load them

The factory branch reads every setting from `ConfigManager`
(`src/kerkov/core/config_manager.py`), so each one needs a field there. Follow
the DeepSeek fields:

```python
    acme_api_key: Optional[str] = Field(None, alias="ACME_API_KEY")
    acme_base_url: str = Field("https://api.acme.example", alias="ACME_BASE_URL")
    default_acme_model: str = Field("acme-large", alias="DEFAULT_ACME_MODEL")
```

The API-key field is the config fallback `_resolve_api_key` receives; without it,
`config.acme_api_key` raises `AttributeError` before the credential manager is
consulted. A keyless backend drops the key field, and a fixed endpoint drops the
base-URL field.

A field alone does not read the environment. `ConfigManager` is a plain Pydantic
`BaseModel`, and `load_config()` builds it from an explicit list of environment
variable names. Add `ACME_API_KEY`, `ACME_BASE_URL`, and `DEFAULT_ACME_MODEL`
to that list; a name left out keeps the field's default even when it is set in
the environment or `.env`.

!!! warning "A new config field needs a documented row"
    The docs lint's `config-settings` check asserts that every `ConfigManager`
    field has a row in `reference/configuration.md`. Each new field therefore
    fails the build until it is documented: the key and endpoint under
    "Provider credentials and endpoints", the model under "Provider and model
    selection". See [Working on the docs](docs.md).

## Checklist

1. `AcmeProvider(BaseLLMProvider)` with the five abstract methods implemented,
   and `caches_prompt_prefix` overridden if the backend caches a prefix.
2. Name added to `SUPPORTED_PROVIDERS`; a branch in `_create_provider`.
3. A `CredentialSpec` registered, for a keyed provider.
4. `ACME_API_KEY`, `ACME_BASE_URL` (if needed), and `DEFAULT_ACME_MODEL` fields
   on `ConfigManager`, listed in `load_config()`, and documented in
   `reference/configuration.md`.
5. Tests under `tests/providers/` named `acme_provider_test.py`.

## Related

- [Providers and models guide](../guides/providers.md) — the operator's view of
  the seven shipped providers.
- [Architecture](../concepts/architecture.md) — where the provider sits in a turn.
- [Context and compaction](../concepts/context-and-compaction.md) — what the
  token-budget methods feed.
