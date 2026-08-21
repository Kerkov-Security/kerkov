# Kerkov

Security-focused conversational AI CLI for authorized penetration testing, security auditing, and CTF competitions. Integrates with LLM providers (Anthropic, Gemini, Ollama, Codex) to provide intelligent security analysis with tool execution capabilities.

## Repo Layout
```
src/
└── kerkov/
    ├── cli/                # Entry point (main.py), conversation loop (kerkov_cli.py),
    │                       # mode_manager, autonomy_manager, input_parser, engagement_store,
    │                       # tool_approval_helper, messages/ (slash-command handlers)
    ├── providers/          # BaseLLMProvider + Anthropic, Gemini, Ollama, Codex (codex/ subpackage),
    │                       # provider_manager (factory/switcher)
    ├── tools/              # BaseTool + active tools: Read, Write, HTTP, Exec, Process, Skill
    │                       # (shell_tool.py and persistent_shell_tools/ exist but are NOT in the active suite)
    ├── agents/             # Agent subsystem: AgentProfile + built-ins (base_agent), AgentManager
    │                       # (AGENT.md loader), ReportTool, AgentRunner, AgentRegistry,
    │                       # SpawnAgentTool (the `delegate` tool wired into the master suite)
    ├── core/
    │   ├── config_manager  # Pydantic config from .env + config/codex.json
    │   ├── conversation_engine  # Shared tool-execution loop; OutputSink/ApprovalStrategy
    │   │                        # abstractions reused by KerkovCLI and AgentRunner
    │   ├── process/        # Supervisor, PTY adapter, session_registry, ANSI strip, key encoding,
    │   │                   # kill_tree - backs ExecTool/ProcessTool
    │   ├── intel/          # CVE intelligence framework: cve_sources (NVD/KEV/EPSS),
    │   │                   # merge, IntelCache (TTL disk) - backs CveLookupTool
    │   ├── shell/          # DEPRECATED (warns on import); do not extend
    │   ├── logging/        # logger_factory
    │   └── json_state.py
    ├── context/            # token_tracker, context_compactor, skills/ (SkillManager + models),
    │                       # prompt templates
    ├── models/             # basic_models.py: Message, LLMResponse, ToolCall, TokenUsage
    └── utils/

config/                 # http_profiles.yaml, codex.json
skills/                 # SKILL.md skills (vulhub-cve-lab, vulhub-exploit-validator,
                        # localhost-scan, r2-analyze, sqli-union)
agents/                 # Optional user-defined AGENT.md profiles (project-root, like skills/)
harnesses/              # Ralph-style autonomous loop runs (e.g. harnesses/runs/<name>/harness.md)
docs/                   # PROJECT_OVERVIEW, internal-dev/, tools/, agent-subsystem.md
tests/                  # Mirrors the src/kerkov package layout
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

`ToolSuite.get_tools()` returns: `ReadTool`, `WriteTool`, `HTTPTool`, `ExecTool`, `ProcessTool`, `ContainerLifecycleTool`, `SbomGenerateTool`, `VulnerabilityScanTool`, `SastScanTool`, `BrowserTool`, `CveLookupTool`, `ZapTool`, and — only when its selected backend is configured — `WebSearchTool` (`web_search`). `KerkovCLI` then appends `SkillTool`, then a `SpawnAgentTool` (the `delegate` tool). Notes:
- `HTTPTool` replaced the old `FetchTool`; supports GET/POST/PUT/PATCH/DELETE plus named credential profiles loaded from `config/http_profiles.yaml`.
- `CveLookupTool` (`cve_lookup`) fuses NVD (CVSS/CWE/references), CISA-KEV (exploited in the wild), and EPSS (exploitation probability) into one `MergedCveRecord` with per-field provenance. The framework lives in `src/kerkov/core/intel/` (sources, merge, TTL cache) mirroring how `core/websearch/` backs `web_search`; the tool is a thin adapter. All sources are keyless so it is registered unconditionally.
- `WebSearchTool` is stage 1 (discovery) of a search→read loop: it returns snippet-level `title/url/snippet` results and is pluggable behind a `SearchBackend` ABC (`TavilyBackend` today). Deep-read the promising URLs with `HTTPTool`.
- `ZapTool` (`zap`) uses the vendor-neutral `core/proxy/` adapter. The first approved spider or active scan generates a managed API key, launches the official stable ZAP Docker image on loopback, and validates readiness before persistence. Read-only status/alert actions auto-approve; crawling, active scans, and stopping scans remain approval-gated.
- `ExecTool` runs one-shot or background commands via the process supervisor; backgrounded sessions are then driven through `ProcessTool` (`list`, `poll`, `log`, `write`, `send_keys`, `paste`, `submit`, `kill`).
- `SbomGenerateTool` (`sbom_generate`) creates Syft, CycloneDX, or SPDX JSON artifacts for workspace files or container images. `VulnerabilityScanTool` (`vulnerability_scan`) runs Grype against workspace directories, images, or an existing SBOM. Syft and Grype must be installed separately and available on `PATH`; Kerkov does not download or update them. Full reports/logs stay in the engagement workspace and compact normalized summaries go to the model.
- `SastScanTool` (`sast_scan`) invokes the open-source OpenGrep binary with workspace-local targets and rules, saves complete JSON/log artifacts, and returns a bounded normalized summary. A working PATH binary is preferred; otherwise an approved scan can install the latest stable official release under `~/.kerkov/tools/opengrep/` after SHA-256 verification. It shares the `EXECUTION_ENABLED` gate with other process-executing tools.
- `ShellTool` and `PersistentShell*` tools still live under `src/kerkov/tools/` but are NOT wired into the active suite — prefer `ExecTool` + `ProcessTool`.
- `delegate` (`SpawnAgentTool`) hands a batch of self-contained sub-tasks to isolated agents; see `docs/agent-subsystem.md`. The master gets approval REQUIRED on the batch; spawned agents auto-approve within their whitelist.

## Providers

Four providers wired via `src/kerkov/providers/provider_manager.py` and `_create_provider`:
`anthropic` (alias `Codex`), `gemini` (aliases `google`, `google-gemini`), `ollama`, `codex` (alias `openai-codex`). Codex auth is OAuth-based and starts automatically when Codex is selected through provider setup; tokens are persisted in `data/providers/codex/auth.json`.

## Modes & Autonomy

Modes (`src/kerkov/cli/modes.py`): `RECON`, `AUDIT` (default), `OFFENSIVE`, `CTF`. The `--mode` CLI flag exposes only recon/audit/offensive; CTF is selectable at runtime via `/mode ctf`.

Autonomy levels (`src/kerkov/cli/autonomy_manager.py`): `1 GUIDED`, `2 STANDARD` (default), `3 AGGRESSIVE`, `4 AUTONOMOUS`. Level 4 requires `ALLOW_AUTONOMOUS=true`.

## Slash Commands

Parsed in `src/kerkov/cli/input_parser.py`: `/mode`, `/model`, `/models`, `/provider`, `/providers`, `/autonomy`, `/clear`, `/q`, `/zap [status|stop]`, `/engagements`, `/resume [id]`, `/skill [list|search <q>|<id> [args]]`. Each maps to a handler under `src/kerkov/cli/messages/`.

## Engagements

Conversations persist as JSONL under `~/.kerkov/engagements/`. Resume with `kerkov --continue` (latest) or `kerkov --resume <id>`. `EngagementStore` handles serialization (including provider-specific raw content blocks).

## Current Product Priorities

Prioritize major tool, engagement persistence, proxy integration, remote
execution, and safety-policy work in this order:

1. Close execution, approval, delegation, scope, and audit safety gaps.
2. Build a typed, redacted engagement ledger separate from conversation JSONL.
3. Maintain the vendor-neutral intercepting-proxy adapter, with OWASP ZAP as the primary backend.
4. Add a local/SSH/container work-host abstraction beneath existing tools.

Do not pursue raw tool-count parity, Mythic integration, or multi-model racer
swarms ahead of those foundations.

## Testing

- **Framework**: unittest (standard library) with `unittest.mock` for mocking. Pytest is also configured in `pyproject.toml` (`testpaths=tests`, `python_files=*_test.py`, `python_classes=*Test`) and can run the same tests.
- **Test file naming**: `*_test.py` (e.g., `key_encoder_test.py`), NOT `test_*.py`
- **Test class naming**: `{Name}Test` (e.g., `StripAnsiTest`, `ChildAdapterTest`)
- **Location**: `tests/` mirrors the `src/kerkov/` package structure
- **Async tests**: Use `asyncio.run()` outside `tests/web/`; pytest manages the loop for native async web tests.
- **Mock helpers**: Define `_create_mock_*()` factory functions at module level
- **Run tests**: `python -m unittest tests/path/to_test.py -v` (or `pytest tests/path/to_test.py`)

## Configuration

`.env` (loaded by `src/kerkov/core/config_manager.py`). Key settings: `LLM_PROVIDER`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `OLLAMA_BASE`, `EXECUTION_ENABLED` (default true), `WRITE_TOOL_ENABLED` (default true), `REQUIRE_SUDO_APPROVAL`, `MAX_CONTEXT_TOKENS` (default 50000), `COMPACTION_THRESHOLD_PCT` (0.65), `SUMMARY_RESERVE_TOKENS` (2000), `MAX_TIMEOUT_SECONDS`, `AUTONOMY_LEVEL`, `ALLOW_AUTONOMOUS`, `HTTP_PROFILES_PATH`. Web search (`WebSearchTool`): `SEARCH_PROVIDER` (default `tavily`), `SEARCH_API_KEY` (the tool is only registered when this is set), `SEARCH_MAX_RESULTS` (10), `SEARCH_TIMEOUT_SECONDS` (15). CVE lookup (`CveLookupTool`): `NVD_API_KEY` (optional — only raises NVD's rate limit), `INTEL_CACHE_DIR` (`~/.kerkov/cache/intel`), `CVE_CACHE_TTL_HOURS` (24), `CVE_LOOKUP_TIMEOUT_SECONDS` (15). Default models: `DEFAULT_ANTHROPIC_MODEL=Codex-sonnet-4-5-20250929`, `DEFAULT_GEMINI_MODEL=gemini-2.5-flash`, `DEFAULT_OLLAMA_MODEL=gpt-oss`, `DEFAULT_CODEX_MODEL=gpt-5-codex`. Codex static OAuth config lives in `config/codex.json`; runtime tokens in `data/providers/codex/auth.json`.

ZAP (`ZapTool`): `ZAP_API_URL` (default `http://127.0.0.1:8090`), `ZAP_API_TIMEOUT_SECONDS` (15), `ZAP_API_VERIFY_TLS` (true), `ZAP_API_ALLOW_REMOTE` (false), `ZAP_MANAGED` (false until Kerkov starts its container), `ZAP_STARTUP_TIMEOUT_SECONDS` (180), `ZAP_DOCKER_IMAGE` (official stable image), and `ZAP_DOCKER_CONTAINER_NAME` (`kerkov-zap`). `ZAP_API_URL` and the non-secret managed-runtime marker may be stored alongside `OLLAMA_BASE` in `~/.kerkov/settings.json`; `ZAP_API_KEY` is managed as a secret and can be replaced with `/auth set zap`.

## Common Commands
```bash
uv sync --locked --extra dev --extra test  # Install with dev/test dependencies
uv run kerkov                              # Run the CLI
uv run kerkov --mode recon --continue
uv run python -m unittest <test_name> -v   # Run a specific test
uv run pytest tests/path/to_test.py        # Or via pytest
black src/ tests/                  # Format
isort src/ tests/                  # Sort imports
mypy src/                          # Type check
bandit -r src/                     # Security lint
```
