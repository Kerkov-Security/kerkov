# Kerkov

Security-focused conversational AI CLI for authorized penetration testing, security auditing, and CTF competitions. Integrates with LLM providers (Anthropic, Gemini, NVIDIA NIM, OpenRouter, Ollama, Codex) to provide intelligent security analysis with tool execution capabilities.

## Repo Layout
```
src/
└── kerkov/
    ├── cli/                # Entry point (main.py), conversation loop (kerkov_cli.py),
    │                       # mode_manager/modes, autonomy_manager, verbosity_manager,
    │                       # approval_mode_manager, approval_rules, tool_approval_helper,
    │                       # input_parser, input_manager, engagement_store,
    │                       # provider_setup_extension, external_prompt_delegate,
    │                       # messages/ (slash-command handlers)
    ├── providers/          # BaseLLMProvider + Anthropic, Gemini, NVIDIA, OpenRouter, Ollama,
    │                       # Codex (codex/ subpackage); __init__ holds SUPPORTED_PROVIDERS and
    │                       # the create_provider_from_name factory; provider_manager (switcher)
    ├── tools/              # BaseTool + active tools: Read, Write, HTTP, Exec, Process,
    │                       # CveLookup, Browser (browser/), WebSearch, Skill
    │                       # (shell_tool.py and persistent_shell_tools/ exist but are NOT in the active suite)
    ├── agents/             # Agent subsystem: AgentProfile + built-ins (base_agent), AgentManager
    │                       # (AGENT.md loader), ReportTool, AgentRunner, AgentRegistry,
    │                       # SpawnAgentTool (the `delegate` tool wired into the master suite)
    ├── core/
    │   ├── config_manager  # Pydantic config from .env + config/codex.json
    │   ├── credentials     # CredentialManager + SecretStore backends (keyring / local file /
    │   │                   # memory); backs runtime key prompts and /auth
    │   ├── conversation_engine  # Shared tool-execution loop; OutputSink/ApprovalStrategy
    │   │                        # abstractions reused by KerkovCLI and AgentRunner
    │   ├── process/        # Supervisor, PTY adapter, session_registry, ANSI strip, key encoding,
    │   │                   # kill_tree - backs ExecTool/ProcessTool
    │   ├── intel/          # CVE intelligence framework: cve_sources (NVD/KEV/EPSS),
    │   │                   # merge, IntelCache (TTL disk) - backs CveLookupTool
    │   ├── websearch/      # SearchBackend ABC + TavilyBackend, SearxngBackend - backs WebSearchTool
    │   ├── browser/        # Browser automation backing BrowserTool
    │   ├── shell/          # DEPRECATED (warns on import); do not extend
    │   ├── logging/        # logger_factory
    │   └── json_state.py, project_paths.py, private_files.py, run_workspace.py
    ├── context/            # token_tracker, context_compactor, skills/ (SkillManager + models)
    ├── web/                # Django/Channels console: project/ (settings, asgi, urls),
    │                       # console/ (consumers, views, routing, security), runtime/
    │                       # (session, approval, output, events)
    ├── models/             # basic_models.py: Message, LLMResponse, ToolCall, TokenUsage
    └── utils/              # privileges.py

config/                 # http_profiles.yaml, approvals.yaml, codex.example.json
                        # (real config/codex.json is untracked)
skills/                 # SKILL.md skills (vulhub-cve-lab, vulhub-exploit-validator,
                        # localhost-scan, r2-analyze, sqli-union,
                        # htb-machine-setup, htb-machine-solve)
agents/                 # Optional user-defined AGENT.md profiles (project-root, like skills/)
docs/                   # PROJECT_OVERVIEW, internal-dev/, tools/, plans/, agent-subsystem.md
tests/                  # Mirrors the src/kerkov package layout (plus tests/web)
```

## Conventions

- **Async everywhere**: All provider `complete()` and tool `execute()` methods are async. Use `asyncio`.
- **Abstract base classes**: Providers extend `BaseLLMProvider`, tools extend `BaseTool`.
- **Pydantic 2.0+** for data models, tool params (`{ToolName}Params`), and config.
- **Tool results**: All tools return `ToolResult(success, output, error, metadata)`.
- **Tool approval levels**: `NONE`, `OPTIONAL`, `REQUIRED` — respect per-tool settings.
- **Type hints required**: mypy strict mode is enabled. Use `Optional[T]` (not `T | None`) for Python 3.10 compat.
- **Formatting**: Black (line-length 88), isort (black profile). Run `black src/ tests/` before committing.
- **Blank lines (PEP 8)**: Two blank lines between top-level definitions (functions, classes); one blank line between methods inside a class. `black` enforces this — run it before committing.
- Known typo: `src/kerkov/providers/anthrophic_provider.py` — do not rename without coordinating.

## Tools (active suite)

`ToolSuite.get_tools()` always returns `ReadTool`, `WriteTool`, `HTTPTool`, `ExecTool`, `ProcessTool`, `CveLookupTool`; it then appends `BrowserTool` (unless `include_browser=False`) and — when the selected search provider can run (Tavily → a saved/environment key or an interactive prompt; SearXNG → `SEARCH_BASE_URL`) — `WebSearchTool` (`web_search`). `KerkovCLI` then appends `SkillTool`, then a `SpawnAgentTool` (the `delegate` tool). Notes:
- `HTTPTool` replaced the old `FetchTool`; supports GET/POST/PUT/PATCH/DELETE plus named credential profiles loaded from `config/http_profiles.yaml`.
- `WebSearchTool` is stage 1 (discovery) of a search→read loop: it returns snippet-level `title/url/snippet` results and is pluggable behind a `SearchBackend` ABC (`TavilyBackend` for the commercial Tavily API, `SearxngBackend` for a self-hosted SearXNG instance). Deep-read the promising URLs with `HTTPTool`.
- `CveLookupTool` (`cve_lookup`) fuses NVD (CVSS/CWE/references), CISA-KEV (exploited in the wild), and EPSS (exploitation probability) into one `MergedCveRecord` with per-field provenance. The framework lives in `src/kerkov/core/intel/` (sources, merge, TTL cache) mirroring how `core/websearch/` backs `web_search`; the tool is a thin adapter. All sources are keyless so it is registered unconditionally.
- `ExecTool` runs one-shot or background commands via the process supervisor; backgrounded sessions are then driven through `ProcessTool` (`list`, `poll`, `log`, `write`, `send_keys`, `paste`, `submit`, `kill`).
- `ShellTool` and `PersistentShell*` tools still live under `src/kerkov/tools/` but are NOT wired into the active suite — prefer `ExecTool` + `ProcessTool`.
- `delegate` (`SpawnAgentTool`) hands a batch of self-contained sub-tasks to isolated agents; see `docs/agent-subsystem.md`. The master gets approval REQUIRED on the batch; spawned agents auto-approve within their whitelist.

## Providers

Six providers listed in `SUPPORTED_PROVIDERS` (`src/kerkov/providers/__init__.py`) and constructed by `create_provider_from_name` there; `ProviderManager` (`provider_manager.py`) owns initialization and runtime switching:
`anthropic` (alias `claude`), `gemini` (aliases `google`, `google-gemini`), `nvidia` (aliases `nim`, `nvidia-nim`), `openrouter` (alias `open-router`), `ollama`, `codex` (alias `openai-codex`). Codex auth is OAuth-based and bootstrapped via the `/codex-login` slash command; tokens are persisted in `data/providers/codex/auth.json`. Other providers resolve their API key from config or the `CredentialManager`, which can prompt interactively and save to keyring/local file.

## Modes & Autonomy

Modes (`src/kerkov/cli/modes.py`): `RECON`, `AUDIT` (default), `OFFENSIVE`, `CTF`. The `--mode` CLI flag exposes only recon/audit/offensive; CTF is selectable at runtime via `/mode ctf`.

Autonomy levels (`src/kerkov/cli/autonomy_manager.py`): `1 GUIDED`, `2 STANDARD` (default), `3 AGGRESSIVE`, `4 AUTONOMOUS`. Level 4 requires `ALLOW_AUTONOMOUS=true`.

Autonomy presets two independent axes, each overridable on its own:
- Verbosity (`verbosity_manager.py`, `/verbosity`, `--verbosity`): `guided`, `standard`, `aggressive`, `silent`.
- Approval gating (`approval_mode_manager.py` + `approval_rules.py`, `/approval`, `--approval`): `strict`, `normal`, `permissive`, `yolo`. `/yolo` and `--yolo` are sugar for `yolo`. Rules load from `config/approvals.yaml`, overridable per-user at `~/.kerkov/approvals.yaml`.

## Slash Commands

Registered in `src/kerkov/cli/input_parser.py`: `/help`, `/prompt`, `/clear`, `/q` (`quit`, `exit`), `/mode`, `/autonomy`, `/verbosity`, `/approval`, `/yolo`, `/provider` (`/providers`), `/model` (`/models`), `/skill [search <q>|<id> [args]]`, `/engagements`, `/resume [id]`, `/auth` (`/credentials`), `/logout` (`/signout`), `/codex-login`. Each maps to a handler under `src/kerkov/cli/messages/`.

## Engagements

Conversations persist as JSONL under `~/.kerkov/engagements/`. Resume with `kerkov --continue` (latest) or `kerkov --resume <id>`. `EngagementStore` handles serialization (including provider-specific raw content blocks).

## Testing

- **Framework**: unittest (standard library) with `unittest.mock` for mocking. Pytest is also configured in `pyproject.toml` (`testpaths=tests`, `python_files=*_test.py`, `python_classes=*Test`) and can run the same tests.
- **Web tests**: Django/Channels tests may use pytest-native async tests and fixtures; run `tests/web/` with pytest because `unittest` does not collect those functions.
- **Test file naming**: `*_test.py` (e.g., `key_encoder_test.py`), NOT `test_*.py`
- **Test class naming**: `{Name}Test` (e.g., `StripAnsiTest`, `ChildAdapterTest`)
- **Location**: `tests/` mirrors the `src/kerkov/` package structure
- **Async tests**: Outside `tests/web/`, use `asyncio.get_event_loop().run_until_complete()`.
- **Mock helpers**: Define `_create_mock_*()` factory functions at module level
- **Run tests**: `python -m unittest tests/path/to_test.py -v` (or `pytest tests/path/to_test.py`)

## Configuration

`.env` (loaded by `src/kerkov/core/config_manager.py`). Key settings: `LLM_PROVIDER` (default `gemini`), `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `NVIDIA_API_KEY`, `NVIDIA_BASE_URL`, `OPENROUTER_API_KEY`, `OPENROUTER_BASE_URL`, `OLLAMA_BASE`, `EXECUTION_ENABLED` (default true), `WRITE_TOOL_ENABLED` (default true), `REQUIRE_SUDO_APPROVAL`, `MAX_CONTEXT_TOKENS` (default 50000), `MAX_RESPONSE_TOKENS`, `COMPACTION_THRESHOLD_PCT` (0.65), `SUMMARY_RESERVE_TOKENS` (2000), `MAX_TIMEOUT_SECONDS` (60), `AUTONOMY_LEVEL` (2), `ALLOW_AUTONOMOUS`, `VERBOSITY`, `APPROVAL_MODE`, `USER_APPROVALS_PATH` (`~/.kerkov/approvals.yaml`), `HTTP_PROFILES_PATH`, `WORKSPACE_DIR` (`~/.kerkov/workspaces`). Agent limits: `AGENT_MAX_DEPTH` (2), `AGENT_MAX_TOTAL` (20), `AGENT_MAX_BATCH` (5), `AGENT_MAX_ITERATIONS` (30), `AGENT_TEMPERATURE`. Credential storage is selected with `KERKOV_CREDENTIAL_STORE` / `KERKOV_CREDENTIAL_FILE` (default `~/.kerkov/credentials.json`). Web search (`WebSearchTool`): `SEARCH_PROVIDER` (default `tavily`; also `searxng`), `SEARCH_API_KEY` (Tavily), `SEARCH_BASE_URL` (SearXNG instance root, e.g. `http://127.0.0.1:8080`; the instance must have `json` in its `search.formats`), `SEARCH_MAX_RESULTS` (10), `SEARCH_TIMEOUT_SECONDS` (15). CVE lookup (`CveLookupTool`): `NVD_API_KEY` (optional — only raises NVD's rate limit), `INTEL_CACHE_DIR` (`~/.kerkov/cache/intel`), `CVE_CACHE_TTL_HOURS` (24), `CVE_LOOKUP_TIMEOUT_SECONDS` (15). Interactive Tavily use can prompt for a missing key; SearXNG requires its base URL before the tool is registered. Default models: `DEFAULT_ANTHROPIC_MODEL=claude-sonnet-4-5-20250929`, `DEFAULT_GEMINI_MODEL=gemini-2.5-flash`, `DEFAULT_NVIDIA_MODEL=z-ai/glm-5.2`, `DEFAULT_OPENROUTER_MODEL=openrouter/auto`, `DEFAULT_OLLAMA_MODEL=gpt-oss`, `DEFAULT_CODEX_MODEL=gpt-5.4`. Codex static OAuth config lives in `config/codex.json` (untracked; see `config/codex.example.json`); runtime tokens in `data/providers/codex/auth.json`.

## Common Commands
```bash
uv sync --locked --extra dev --extra test  # Install with dev/test dependencies
uv run kerkov                              # Run the CLI
uv run kerkov --mode recon --continue
uv run kerkov --message "scan 10.0.0.1" --no-welcome  # Non-interactive (repeatable --message)
uv run kerkov-web runserver                # Django/Channels web console
uv run python -m unittest <test_name> -v   # Run a specific test
uv run pytest tests/path/to_test.py        # Or via pytest
black src/ tests/                  # Format
isort src/ tests/                  # Sort imports
mypy src/                          # Type check
bandit -r src/                     # Security lint
```
