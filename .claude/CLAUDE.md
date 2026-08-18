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
    │                       # CveLookup, Browser (browser/), WebSearch, Skill, Recall
    │                       # (shell_tool.py and persistent_shell_tools/ exist but are NOT in the active suite)
    ├── agents/             # Agent subsystem: AgentProfile + built-ins (base_agent), AgentManager
    │                       # (AGENT.md loader), ReportTool, AgentRunner, AgentRegistry,
    │                       # SpawnAgentTool (`delegate`), AgentSupervisor + AgentSession
    │                       # (detached runs), AgentWaitTool (`agent_wait`), report_format
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
    │   ├── knowledge_base/ # EngagementKnowledgeBase (SQLite/FTS5 artifact store),
    │   │                   # models, EmbeddingBackend ABC - backs RecallTool
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
- **Comments are sparse.** The code is expected to explain itself; write it so it does. Add a `#` comment only where something is genuinely non-obvious — a workaround, a non-local constraint, a subtle ordering requirement, a "why this and not the obvious thing". Never restate what the next line already says, and never narrate a block that reads fine on its own. If a comment feels needed to make code followable, prefer a clearer name or a smaller function.
- **Docstrings are one line.** A single summary line is the default for modules, classes, and functions. Expand past that only when a caller genuinely cannot use the thing correctly without it (a non-obvious contract, a surprising failure mode) — and then add the minimum, not a full Args/Returns treatment that duplicates the type hints. Multi-paragraph docstrings are rare and should feel earned.
- Known typo: `src/kerkov/providers/anthrophic_provider.py` — do not rename without coordinating.

## Tools (active suite)

`ToolSuite.get_tools()` always returns `ReadTool`, `WriteTool`, `HTTPTool`, `ExecTool`, `ProcessTool`, `CveLookupTool`; it then appends `BrowserTool` (unless `include_browser=False`) and — when the selected search provider can run (Tavily → a saved/environment key or an interactive prompt; SearXNG → `SEARCH_BASE_URL`) — `WebSearchTool` (`web_search`). `KerkovCLI` then appends `SkillTool`, `RecallTool` and `LedgerTool` (both when `KB_ENABLED`), then a `SpawnAgentTool` (the `delegate` tool) and an `AgentWaitTool` (`agent_wait`). Notes:
- `HTTPTool` replaced the old `FetchTool`; supports GET/POST/PUT/PATCH/DELETE plus named credential profiles loaded from `config/http_profiles.yaml`.
- `WebSearchTool` is stage 1 (discovery) of a search→read loop: it returns snippet-level `title/url/snippet` results and is pluggable behind a `SearchBackend` ABC (`TavilyBackend` for the commercial Tavily API, `SearxngBackend` for a self-hosted SearXNG instance). Deep-read the promising URLs with `HTTPTool`.
- `CveLookupTool` (`cve_lookup`) fuses NVD (CVSS/CWE/references), CISA-KEV (exploited in the wild), and EPSS (exploitation probability) into one `MergedCveRecord` with per-field provenance. The framework lives in `src/kerkov/core/intel/` (sources, merge, TTL cache) mirroring how `core/websearch/` backs `web_search`; the tool is a thin adapter. All sources are keyless so it is registered unconditionally.
- `ExecTool` runs one-shot or background commands via the process supervisor; backgrounded sessions are then driven through `ProcessTool` (`list`, `poll`, `log`, `write`, `send_keys`, `paste`, `submit`, `kill`).
- `ShellTool` and `PersistentShell*` tools still live under `src/kerkov/tools/` but are NOT wired into the active suite — prefer `ExecTool` + `ProcessTool`.
- `recall` (`RecallTool`) searches the engagement knowledge base — every tool result gathered so far, indexed by the ingestion hook in `conversation_engine`. Snippet-first like `web_search`; `full=true` or a query of `artifact#N` expands one record. Registered at CLI level (not in `ToolSuite`) because it needs a knowledge base keyed by the live `engagement_id`, which does not exist when tools are built — so it takes a getter rather than the store itself. Sits in the master tools snapshot, so an agent whose profile whitelists `recall` receives it — every built-in profile does; a user-defined `AGENT.md` has to name it in `allowed_tools` like any other tool.
- `ledger` (`LedgerTool`) writes to the findings ledger — the model's structured, always-in-context working memory (`hosts/services/credentials/findings/flags/next_steps`). `op=add|list|remove` (no `update` in v1). Registered next to `recall` — on the CLI and in the web console — and gated on the same `KB_ENABLED`; late-bound getters supply the `FindingsLedger` and the writing engagement id. `add` requires the section's identity fields (see below) so entries merge instead of forking. Auto-approved (writes only to local engagement state). Reaches agents via the master-tools snapshot like `recall`. See "Findings Ledger" below.
- `delegate` (`SpawnAgentTool`) hands a batch of self-contained sub-tasks to isolated agents and **returns immediately** with their ids (`a1`, `a2`, …) — the agents run detached as supervised `asyncio.Task`s so the master keeps its prompt. See `docs/agent-subsystem.md`. The master gets approval REQUIRED on the batch; spawned agents auto-approve within their whitelist.
- Agents carry two handles: an internal `id` (`a1`, monotonic, never reused, what `agent_wait`/logs key on) and a user-facing `name` (`google-pinger`). `delegate` takes an optional `name` per task so the master names it in the same call — no extra LLM round-trip; unnamed tasks fall back to a slug of the instruction, then the profile. Names are slugified and deduped per session; `supervisor.get()` and `wait(ids=…)` resolve either handle. **CLI output prints the name only**; model-facing text carries `name (id, profile)`.
- `agent_wait` (`AgentWaitTool`) is the collection half: it blocks on named (or all outstanding) agents and returns their reports. Auto-approved. A `timeout_s` returns the ready reports plus a `Still running: …` line rather than raising. The safety net for a master that never calls it is **turn-boundary delivery** — each front end drains `supervisor.drain_finished_reports()` before a new turn and appends each undelivered report as a `user` message, exactly once (`AgentSession.report_delivered`). Both paths render through `agents/report_format.py`, so the model sees one shape.
- `AgentSupervisor` (session-scoped, one per `KerkovCLI` / `WebKerkovSession`) owns the live agents: task handles, monotonic never-reused ids, the shared workspace, `cancel`/`cancel_all`, and report delivery. `AgentRegistry` stays the budget *policy* (depth/total); the supervisor enforces `AGENT_MAX_CONCURRENT` over live instances, reusing `BudgetExceeded` so `delegate` keeps one error path. Quit paths (`/q`, `Ctrl-C`, EOF) must `await cancel_all()` or agents are orphaned; a `--message` one-shot instead waits for them, records the reports in the transcript, and prints them, since there is no next turn.
- **Master and all agents in a tree share one run workspace**, keyed on the *root* engagement id (same rule as the KB store) and injected into every `AgentRunner` by the supervisor — a per-agent directory would be invisible to the master and to siblings. Tradeoff: concurrent agents can clobber files by name; the mitigation is a prompt convention to namespace outputs, not enforced isolation.

## Providers

Six providers listed in `SUPPORTED_PROVIDERS` (`src/kerkov/providers/__init__.py`) and constructed by `create_provider_from_name` there; `ProviderManager` (`provider_manager.py`) owns initialization and runtime switching:
`anthropic` (alias `claude`), `gemini` (aliases `google`, `google-gemini`), `nvidia` (aliases `nim`, `nvidia-nim`), `openrouter` (alias `open-router`), `ollama`, `codex` (alias `openai-codex`). Codex auth is OAuth-based and starts automatically when Codex is selected through provider setup; tokens are persisted in `data/providers/codex/auth.json`. Other providers resolve their API key from config or the `CredentialManager`, which can prompt interactively and save to keyring/local file.

## Modes & Autonomy

Modes (`src/kerkov/cli/modes.py`): `RECON`, `AUDIT` (default), `OFFENSIVE`, `CTF`. The `--mode` CLI flag exposes only recon/audit/offensive; CTF is selectable at runtime via `/mode ctf`.

Autonomy levels (`src/kerkov/cli/autonomy_manager.py`): `1 GUIDED`, `2 STANDARD` (default), `3 AGGRESSIVE`, `4 AUTONOMOUS`. Level 4 requires `ALLOW_AUTONOMOUS=true`.

Autonomy presets two independent axes, each overridable on its own:
- Verbosity (`verbosity_manager.py`, `/verbosity`, `--verbosity`): `guided`, `standard`, `aggressive`, `silent`.
- Approval gating (`approval_mode_manager.py` + `approval_rules.py`, `/approval`, `--approval`): `strict`, `normal`, `permissive`, `yolo`. `/yolo` and `--yolo` are sugar for `yolo`. Rules load from `config/approvals.yaml`, overridable per-user at `~/.kerkov/approvals.yaml`.

## Slash Commands

Registered in `src/kerkov/cli/input_parser.py`: `/help`, `/prompt`, `/clear`, `/q` (`quit`, `exit`), `/mode`, `/autonomy`, `/verbosity`, `/approval`, `/yolo`, `/provider` (`/providers`), `/model` (`/models`), `/skill [search <q>|<id> [args]]`, `/engagements`, `/resume [id]`, `/auth` (`/credentials`), `/logout` (`/signout`), `/zap [status|stop]`. Each maps to a handler under `src/kerkov/cli/messages/`.

## Engagements

Conversations persist as JSONL under `~/.kerkov/engagements/`. Resume with `kerkov --continue` (latest) or `kerkov --resume <id>`. `EngagementStore` handles serialization (including provider-specific raw content blocks).

## Knowledge Base (`recall`)

Alongside the JSONL, each engagement gets a SQLite/FTS5 artifact store at `~/.kerkov/engagements/<root_engagement_id>.kb.sqlite`, created lazily on first write. `ConversationEngine` indexes every tool result on its way into history and, when the result exceeds `KB_CLIP_THRESHOLD_BYTES`, replaces it in the conversation with an `artifact#N` reference plus a head/tail preview. Small results stay inline but are still indexed. The threshold is measured over output *and* metadata (`history_footprint_bytes`), and clipping elides oversized metadata values too (`build_clipped_metadata`) — `format_tool_result` renders metadata into history, and `HTTPTool` parks a parsed copy of the response body there, so clipping output alone would put the whole response straight back.

`recall(source=...)` is a substring matched against both the producing tool name and the artifact title, which is where the target lands (`http: <url>`, `read: <path>`) — the source column itself only ever holds the tool name, so a host or URL filter would otherwise silently return nothing.

Artifacts are classified by `ArtifactKind` (`core/knowledge_base/models.py`), assigned from the producing tool by `artifact_kind_for_tool`: `read` → `FILE`, `http` → `HTTP_RESPONSE`, everything else → `TOOL_OUTPUT`. Only kinds something can actually produce are members — an unreachable value in the enum becomes a `recall(kind=...)` filter that returns confidently empty results forever. `credential` and `note` are deliberately absent from `ArtifactKind`; those facts live in the findings ledger (see below) as its own sections, not as artifact kinds.

**One store per engagement *tree*, not per engagement id.** A spawned agent gets its own `engagement_id`, so keying the file on that id would give each agent a private database its master could never read. Instead `engagement_id` is a *column*, the file is named for the root, and `SpawnAgentTool` passes the master's `EngagementKnowledgeBase` down to `AgentRunner` rather than letting agents open their own. `recall` searches the whole tree by default and takes an optional `engagement` filter — which is how the master drills into the raw output behind a delegated agent's summarized FINDINGS, using the id the report formatter prints next to them.

Resolving which store an engagement belongs to walks `parent_engagement_id` to the root (`resolve_root_engagement_id`). Note the current spawn wiring hands nested spawn tools the *master's* store, so every agent records the master as its parent regardless of depth and the walk resolves in one hop; it is written as a bounded loop so a later fix to that parentage keeps working. `delete_engagement` deletes the `.kb.sqlite` only for a root — deleting an agent sub-run drops just its rows, since the file belongs to its master.

The store is plaintext on local disk and holds whatever the tools returned, harvested credentials included. Nothing is redacted and nothing leaves the machine.

## Findings Ledger (`ledger`)

A structured store of confirmed facts (`hosts / services / credentials / findings / flags / next_steps`) that the model writes to via the `ledger` tool and that is **re-injected at the top of context every turn**, so it survives context compaction verbatim — the fix for "forgot the credential found 40 messages ago". It lives in the **same `.kb.sqlite`** as `recall` (table `ledger_entries`, added in schema v2; an existing v1 DB migrates in place, keeping its artifacts), so it inherits the KB's root-keying, WAL/`busy_timeout` concurrency, and lifecycle. `EngagementKnowledgeBase` owns the SQL; `FindingsLedger` (`core/knowledge_base/findings_ledger.py`) owns the domain logic — per-section dedup identity, merge-on-conflict (new detail enriches a row rather than duplicating it), and a compact, byte-stable serialization cached on `ledger_revision()`.

Identity is required, not guessed: `identity_error()` rejects an `add` that cannot be identified (hosts need ip-or-hostname, services host+port, credentials username plus host-or-source, findings a title, flags a value, next_steps a step) because a hash-fallback row never merges with its own later enrichment, and host-less services would all collapse onto the same port. Identity still firms up over time, so `_identity()` also returns *alias* keys (the hostname a host was first known by, the `source` before a `host`, the credential recorded before its `kind`); `add_ledger_entry` looks rows up under key-or-alias, folds duplicates into the oldest row and promotes it to the canonical key. On merge, `engagement_id` and `artifact_ref` are **insert-only**: the first contributor keeps ownership — `delete_engagement_ledger_rows` deletes by owner, so reassigning it would let an enriching agent's cleanup take the master's facts — and a later detail-only add never clears an evidence link.

`op=add|list|remove` only — **no `update` in v1** (it is `remove`+`add`; deviates from the issue's acceptance criteria). Injection is transient: `ConversationEngine` re-serializes the ledger at the top of each loop iteration into `LoopSession.ledger_block` and `_messages_for_provider` inserts it as a `system` message right after the base prompt. It is never written into `history`, so `ContextCompactor` never sees it — no exclusion code needed. Because the block is outside compaction it cannot be reclaimed once it grows, so `serialize()` trims to `max_injected_bytes` (`DEFAULT_MAX_INJECTED_BYTES`, 16000 ≈ 4k tokens) dropping whole-section-priority-last first (`_SECTION_PRIORITY`: credentials/flags/findings survive host and service enumeration) and appends a count of what it omitted; nothing is deleted, `ledger op=list` still reads everything. With a ledger actually open, the compaction prompt is softened (`ledger_active` flag) to lean on narrative/attack-chain reasoning rather than exhaustively re-listing creds/IPs/flags, while still keeping anything not obviously a ledger fact — the flag tracks the live `FindingsLedger`, not `KB_ENABLED`, since a store that failed to open would otherwise license the summarizer to drop the only copy.

Both front ends wire it: `KerkovCLI` and `WebKerkovSession` each build the `FindingsLedger` beside the knowledge base, register `LedgerTool` under `KB_ENABLED`, and pass `ledger=` into their `LoopSession` — the mode prompt instructs the model to record facts, so a front end without the tool would be advertising something it does not have.

Tree-shared like the KB: spawned agents both write to and receive the injected ledger. The `ledger` tool reaches agents via the master-tools snapshot; because tools pass by reference, an agent's writes are recorded under the master's `engagement_id` (cosmetic — still tree-shared and recalled). Gated on the same `KB_ENABLED` (no separate flag) and auto-approved in `config/approvals.yaml` — and in its packaged twin `src/kerkov/config/approvals.yaml`, which `default_approvals_source` falls back to outside a source checkout, so the two must be kept in step. Secrets are stored and re-injected **unmasked** — same cleartext/local-only/no-telemetry posture as the KB. `delete_engagement` drops an agent sub-run's ledger rows (`delete_engagement_ledger_rows`) and takes the whole file with a root delete.

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

`.env` (loaded by `src/kerkov/core/config_manager.py`). Key settings: `LLM_PROVIDER` (default `gemini`), `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `NVIDIA_API_KEY`, `NVIDIA_BASE_URL`, `OPENROUTER_API_KEY`, `OPENROUTER_BASE_URL`, `OLLAMA_BASE`, `EXECUTION_ENABLED` (default true), `WRITE_TOOL_ENABLED` (default true), `REQUIRE_SUDO_APPROVAL`, `MAX_CONTEXT_TOKENS` (default 50000), `MAX_RESPONSE_TOKENS`, `COMPACTION_THRESHOLD_PCT` (0.65), `SUMMARY_RESERVE_TOKENS` (2000), `MAX_TIMEOUT_SECONDS` (60), `AUTONOMY_LEVEL` (2), `ALLOW_AUTONOMOUS`, `VERBOSITY`, `APPROVAL_MODE`, `USER_APPROVALS_PATH` (`~/.kerkov/approvals.yaml`), `HTTP_PROFILES_PATH`, `WORKSPACE_DIR` (`~/.kerkov/workspaces`). Agent limits: `AGENT_MAX_DEPTH` (2), `AGENT_MAX_TOTAL` (20), `AGENT_MAX_BATCH` (5), `AGENT_MAX_CONCURRENT` (5 — live agents at once, enforced by `AgentSupervisor`), `AGENT_MAX_ITERATIONS` (30), `AGENT_TEMPERATURE`. Knowledge base (`recall`): `KB_ENABLED` (true — master switch for ingestion and the tool), `KB_CLIP_THRESHOLD_BYTES` (8000 — output above this is stored and referenced instead of inlined), `KB_EMBEDDINGS` (false — opt-in semantic recall; default is FTS5-only and fully offline), `KB_EMBEDDING_PROVIDER` (backend name; no concrete backend ships yet, so it is currently inert). The findings ledger (`ledger`) shares `KB_ENABLED` — no separate flag. Credential storage is selected with `KERKOV_CREDENTIAL_STORE` / `KERKOV_CREDENTIAL_FILE` (default `~/.kerkov/credentials.json`). Web search (`WebSearchTool`): `SEARCH_PROVIDER` (default `tavily`; also `searxng`), `SEARCH_API_KEY` (Tavily), `SEARCH_BASE_URL` (SearXNG instance root, e.g. `http://127.0.0.1:8080`; the instance must have `json` in its `search.formats`), `SEARCH_MAX_RESULTS` (10), `SEARCH_TIMEOUT_SECONDS` (15). CVE lookup (`CveLookupTool`): `NVD_API_KEY` (optional — only raises NVD's rate limit), `INTEL_CACHE_DIR` (`~/.kerkov/cache/intel`), `CVE_CACHE_TTL_HOURS` (24), `CVE_LOOKUP_TIMEOUT_SECONDS` (15). Interactive Tavily use can prompt for a missing key; SearXNG requires its base URL before the tool is registered. Default models: `DEFAULT_ANTHROPIC_MODEL=claude-sonnet-4-5-20250929`, `DEFAULT_GEMINI_MODEL=gemini-2.5-flash`, `DEFAULT_NVIDIA_MODEL=z-ai/glm-5.2`, `DEFAULT_OPENROUTER_MODEL=openrouter/auto`, `DEFAULT_OLLAMA_MODEL=gpt-oss`, `DEFAULT_CODEX_MODEL=gpt-5.4`. Codex static OAuth config lives in `config/codex.json` (untracked; see `config/codex.example.json`); runtime tokens in `data/providers/codex/auth.json`.

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
