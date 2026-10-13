# Kerkov

Security-focused conversational AI CLI and local web console for
authorized penetration testing, security auditing, and CTF workflows.

## Working in this repository

- Treat the implementation, `pyproject.toml`, and CI workflows as the source of
  truth for current behavior. Design plans and older docs can describe work that
  is incomplete or has since changed.
- Follow the requested scope. Extend the existing execution, approval, persistence,
  and proxy abstractions before adding parallel implementations.
- Preserve unrelated local changes and keep formatting limited to files you edit.
  Update relevant tests and user-facing docs when behavior changes.

## Repository map

Paths below are relative to the repository root.

| Path | Responsibility |
| --- | --- |
| `src/kerkov/cli/` | `main.py` entry point, `kerkov_cli.py` runtime, input and command handlers, approval rules, engagement JSONL |
| `src/kerkov/web/` | Django + Channels console; `runtime/` adapts the shared engine to WebSocket events and approvals |
| `src/kerkov/core/conversation_engine.py` | Shared loop for CLI, web sessions, and agents; `LoopSession`, `OutputSink`, `ApprovalStrategy` |
| `src/kerkov/providers/` | Provider implementations; factories and aliases in `__init__.py`, switching in `provider_manager.py` |
| `src/kerkov/tools/` | `BaseTool`, tool implementations, and `tool_suite.py` registration |
| `src/kerkov/agents/` | Profiles, runners, budget registry, detached sessions, supervisor, delegation and report tools |
| `src/kerkov/core/process/` | Process supervisor, session registry, POSIX/Windows adapters, terminal rendering and cleanup |
| `src/kerkov/core/knowledge_base/` | SQLite/FTS5 artifacts, optional embeddings, and structured findings ledger |
| `src/kerkov/core/proxy/`, `core/intel/`, `core/websearch/` | ZAP adapter/runtime, CVE sources/cache, and search backends (all under `src/kerkov/`) |
| `src/kerkov/core/` | Configuration, credentials/private files, project paths, run workspaces, JSON state and logging |
| `src/kerkov/context/`, `src/kerkov/models/` | Token tracking, compaction, skill loading; shared message/response models |
| `config/`, `src/kerkov/config/` | Project config/examples; packaged approval defaults |
| `skills/`, `agents/` | Application `SKILL.md` skills and optional `AGENT.md` profiles |
| `image/`, `compose.yaml` | Container image/runtime smoke test and local Kerkov + ZAP stack |
| `tests/` | Mirrors the package layout; includes CLI, web, tools, providers, agents and core tests |
| `docs/` | Project overview, tool docs, agent subsystem, architecture notes and plans |

## Implementation conventions

- Support Python `>=3.11,<3.15`; `.python-version` pins 3.13 for development/CI.
  Add type hints and follow nearby style. Both `Optional[T]` and `T | None` work
  on Python 3.11. The configured mypy checks are in `pyproject.toml`.
- Providers extend `BaseLLMProvider`; `complete()` is async. Keep blocking SDK,
  network and process work off the shared event loop.
- Tools extend `BaseTool`. Define Pydantic 2 `ToolParams` with `Field` metadata,
  implement `get_params_class()`, `validate_params()` and async `run()`, and
  return `ToolResult`. The inherited `execute()` validates inputs and manages
  the execution call ID; preserve that wrapper.
- Keep shared conversation behavior in `ConversationEngine`; route presentation
  and approval interactions through the sink/strategy interfaces. Check CLI,
  web and agent callers when changing their shared contracts.
- Use Black (88 columns) and isort (Black profile). CI explicitly checks PEP 8
  blank lines with Flake8.
- The filename `src/kerkov/providers/anthrophic_provider.py` is an existing typo;
  avoid renaming it as incidental cleanup.

## Tool and provider wiring

- `ToolSuite.get_tools()` registers `ReadTool`, `WriteTool`, `HTTPTool`,
  `ExecTool`, `ProcessTool`, `ContainerLifecycleTool`, `SbomGenerateTool`,
  `VulnerabilityScanTool`, `SastScanTool`, `CveLookupTool` and `ZapTool`.
  `BrowserTool` is optional and the web runtime disables it.
- `WebSearchTool` registration is provider-aware: Tavily needs a configured key
  or an interactive credential manager/prompt; SearXNG needs `SEARCH_BASE_URL`.
  Search returns discovery snippets; use `HTTPTool` to read selected URLs.
  `/searxng setup` provisions the keyless SearXNG backend end to end
  (`core/websearch/searxng_runtime.py`), including the `search.formats: json`
  entry a stock instance omits, without which every request is answered 403.
  Setup/start/stop reload search tools in the CLI and the shared pool for future
  agents; no restart is needed. Existing agent runs keep their current tools.
- CLI and web runtimes add `SkillTool`, `SpawnAgentTool` (`delegate`) and
  `AgentWaitTool` (`agent_wait`). They add `RecallTool` and `LedgerTool`
  when `KB_ENABLED` is true. Check both runtimes when changing registration.
- Use `ExecTool` + `ProcessTool` for commands and ongoing sessions, and
  `ContainerLifecycleTool` for Docker Compose lifecycle/readiness. The legacy
  shell stack (`shell_tool.py`, `persistent_shell_tools/`, `core/shell/`) has
  been deleted; do not reintroduce it for new execution features.
- `SbomGenerateTool` (`sbom_generate`) creates Syft, CycloneDX or SPDX JSON
  artifacts for workspace files or container images. `VulnerabilityScanTool`
  (`vulnerability_scan`) uses Grype for workspace directories, images or an
  existing SBOM. Both require separately installed binaries on `PATH`; scans
  do not install or update them. Full reports/logs stay in the engagement
  workspace and compact normalized summaries go to the model.
- `SastScanTool` (`sast_scan`) uses an existing OpenGrep binary with explicit
  host-local targets and rules. Absolute paths are accepted; relative paths
  stay workspace-confined. Full JSON/log artifacts stay in the workspace and
  the model receives a redacted bounded summary. `kerkov setup opengrep`
  explicitly installs a verified user-local runtime; scans never install
  software. These scanner tools respect `EXECUTION_ENABLED`.
- Keep tool adapters thin: CVE intelligence belongs in `core/intel/`, search in
  `core/websearch/`, and ZAP integration behind the vendor-neutral
  `core/proxy/adapter.py`. Preserve process ownership and cancellation cleanup.
- Supported provider names are `anthropic`, `gemini`, `nvidia`, `openrouter`,
  `deepseek`, `ollama` and `codex`. Anthropic's alias is `claude`. Read the factory for
  all aliases and `ConfigManager` for default models instead of copying model
  IDs into this guide.
- **Prompt-caching prefix stability (Anthropic).** `AnthropicProvider` emits two
  ephemeral cache breakpoints — one on the stable system+tools prelude, one
  rolling onto the last content block of the most recent turn (`_build_request`).
  A cache hit requires the prefix (tools, system, prior history) to be
  **byte-identical** across turns, so nothing volatile may live inside it: keep
  timestamps, engagement IDs, target hosts and per-turn interpolation out of the
  system prompt and tool schemas, and keep tool serialization deterministic
  (`_convert_to_anthropic_tool_schema` already builds from ordered dicts). The
  byte-stability regression tests live in `tests/providers/anthropic_caching_test.py`;
  run them after touching prompt or tool-schema assembly.
- Mutable working state rides the tail. The findings ledger follows stable
  history as the final message for prefix-caching providers
  (`BaseLLMProvider.caches_prompt_prefix`, currently Anthropic and DeepSeek), after
  Anthropic's last cache breakpoint. Other providers keep it at the front
  (`ConversationEngine._messages_for_provider`).
  This trades the ledger's head-position salience for recency salience on
  Anthropic and DeepSeek so ledger updates preserve the earlier prefix; do not move
  volatile content back into the prefix.
- Cache-aware accounting: Anthropic's `usage.input_tokens` is only the
  uncached remainder, so `_usage_from_message` sets `prompt_tokens =
  input_tokens + cache_read_input_tokens + cache_creation_input_tokens` and
  recomputes `total_tokens` from it — `TokenTracker`/compaction must still see
  the full prompt size. DeepSeek already includes cache hits in `prompt_tokens`;
  report its cache hits separately without adding them again. Never substitute a
  local token estimator.
- **Streaming assistant output.** `provider.complete()` takes an optional
  `on_delta: Callable[[str], None]`. When supplied, the provider forwards
  assistant text chunks as they arrive and still returns the identical final
  `LLMResponse`; when `None`, it accumulates internally (the cheap path). The
  engine only threads a callback when `OutputSink.supports_streaming` is True —
  `ConsoleSink` (and `BufferingSink`, which delegates while live and drops
  deltas while buffering an attached agent) and `WebOutputSink` support streaming
  according to `STREAM_OUTPUT`; `QuietSink`/agents return False. `ConsoleSink`
  suppresses re-rendering the completed Markdown when it equals the text it
  already streamed (`_consume_streamed`), avoiding a double print. The OpenAI-
  compatible providers (NVIDIA, OpenRouter) reassemble the SSE stream into the
  same response dict via `providers/openai_stream.py`, so tool-call parsing is
  unchanged; Ollama folds its NDJSON stream; Gemini uses `stream=True`. Keep the
  streamed and non-streamed paths producing the same `LLMResponse`.
- **Interrupt mid-stream.** `_complete` accumulates the streamed chunks; on
  `InterruptedByUser` it attaches them as `partial_text`, and `run_loop` records
  a `Message(role="assistant", partial=True)` so the visible text is not lost.
  The master ends the turn (returns `None`); agents (no streaming) are
  unaffected. `Message.partial` persists through `EngagementStore`.

## Detached agents

- At the master level, `delegate` returns agent IDs immediately. Reports arrive
  through `agent_wait` or supervisor delivery at the next turn boundary.
  Nested delegation blocks on its own children so their findings reach the
  parent before it reports.
- `/agents` lists agents or attaches to a named agent's read-only output view;
  `/back` returns to the master. Focus changes must not restart agent work.
- `AgentRegistry` enforces depth/total budgets; `AgentSupervisor` owns live
  sessions, concurrency, cancellation and report delivery. Preserve batch and
  iteration caps as well as exactly-once report delivery.
- Each agent has separate history and engagement JSONL, but the engagement tree
  shares the root workspace and knowledge base. A task's `allowed_tools` may
  narrow the selected profile's direct tool set. Nested delegation is an
  exception: `_build_runner()` adds `delegate` when depth permits, regardless
  of the profile's allowlist, and supplies it with the master's tool pool.
  Agents use `WhitelistAutoApproval`, so a restricted parent can spawn a more
  capable child profile without another user approval. Profile allowlists do
  not enforce permission limits across descendants.
- Preserve existing shutdown and conversation-switch cleanup, but account for
  its current gaps: CLI `/clear` and `/resume` settle agents and discard pending
  reports. Web `/clear` and `/mode` also retire the supervisor and its callbacks
  before resetting the Agents panel, while retaining the lifetime spawn budget.
  CLI `/mode` still resets history without that cleanup, so an old agent's report
  can be persisted into the new engagement. Changes to that path should settle
  agents and discard pending reports before switching.
- See `docs/agent-subsystem.md` for lifecycle details, checked against the
  implementation and the limitations above. Use the approval implementation
  below for current gating behavior.

## Modes, commands and approvals

- Operational modes are `RECON`, `AUDIT` (default), `OFFENSIVE` and `CTF`.
  Startup `--mode` supports the first three; `/mode ctf` is available at runtime.
- Verbosity and approval mode are independent axes. `/autonomy` and
  `AUTONOMY_LEVEL` apply legacy presets to both. Level 4 and approval mode
  `yolo` require the autonomous opt-in; see the managers and CLI overrides.
- Approval is rules-based, not a per-tool `NONE/OPTIONAL/REQUIRED` enum.
  Rules use first-match order: workspace, session, user, then project defaults.
  `strict` prompts unless the matching rule denies; `normal` honors rules and
  prompts on unmatched calls; `permissive` auto-approves unmatched calls;
  `yolo` bypasses rules/prompts. The CLI also has an explicit noninteractive
  auto-approval path in `ToolApprovalCliHelper`.
- When changing default rules, keep `config/approvals.yaml` and
  `src/kerkov/config/approvals.yaml` aligned so source and installed-package
  behavior agree. Check both the CLI helper and `web/runtime/approval.py`.
- `src/kerkov/cli/input_parser.py:COMMANDS` defines command dispatch and help
  metadata. Add or change commands there and in `cli/messages/`; avoid a
  second command list. Current commands include credential management,
  verbosity/approval, prompt-file loading, skills, providers/models and resumes.

## Persistence and credentials

- `EngagementStore` persists transcripts under `~/.kerkov/engagements/`.
  Preserve metadata compatibility and provider-specific raw message blocks on
  save/resume. `--continue` resumes the latest conversation; `--resume <id>`
  selects one explicitly.
- `RunWorkspaceManager` manages `~/.kerkov/workspaces/<root-engagement-id>/`
  by default (`WORKSPACE_DIR` overrides the root). Use its context-local helpers
  and path validation when accessing artifacts from concurrent runs.
- `EngagementKnowledgeBase` stores artifacts in
  `<root-engagement-id>.kb.sqlite` alongside transcripts. `recall` retrieves
  full tool output clipped from model history. `FindingsLedger` already exists
  in that database and injects bounded structured working memory outside
  compactable history; preserve deduplication, provenance and resume behavior.
  This is working memory that can contain discovered credentials, not a
  redacted audit log.
- `core/config_manager.py` and `.env.example` document settings.
  `CredentialManager` handles API secrets through masked prompts, session
  storage, OS keyring or a protected fallback file. Reuse the credential,
  private-file and subprocess-environment helpers; keep provider/tool API keys
  out of chat, logs and ordinary settings.
- Codex static configuration is copied from `config/codex.example.json` to
  `config/codex.json`; OAuth state defaults to
  `data/providers/codex/auth.json`. Non-secret runtime settings live in
  `~/.kerkov/settings.json`. Keep generated state and secrets out of commits.
- The web console is loopback-only and has no user-account
  authentication. Preserve its host/origin/access-token checks and session
  cleanup. The web runtime consumes existing/environment credentials and
  does not expose the CLI's masked credential-entry flow.

## Development and verification

```bash
uv sync --locked --extra dev --extra test --extra web
uv run kerkov --help
uv run kerkov
uv run --no-sync kerkov-web runserver
uv run python -m pytest tests/tools/tool_suite_test.py  # Example focused test
uv run python -m flake8 --select=E301,E302,E303,E305,E306 src tests
uv run python -m pytest tests                         # Full CI test command
```

- Include the `web` extra for the complete test suite; CLI-only installs can
  omit it. Keep `uv.lock` consistent with intentional dependency changes.
- Pytest runs both unittest-style cases and pytest functions. Test files use
  `*_test.py`, functions use `test_*`, and pytest-style classes use `*Test`.
  Follow the surrounding test style.
- For async tests outside `tests/web/`, use an explicit `asyncio.run()` wrapper.
  Pytest manages the loop for native async web tests (`@pytest.mark.asyncio`).
  Avoid assuming `asyncio.get_event_loop()` will create a loop.
- Use `unittest.mock`/`AsyncMock` or pytest fixtures for external services.
  Isolate filesystem/configuration state with temporary paths and in-memory
  credential stores rather than the developer's real `~/.kerkov` or keyring.
- Format edited Python files with `uv run black <paths>` and
  `uv run isort <paths>`. Mypy and Bandit are available for relevant changes
  (`uv run mypy src/`, `uv run bandit -r src/`); they are not current CI gates.
- Container build CI is currently disabled; its definition is preserved in
  `.github/workflows/build-kerkov.yml.disabled`. Container changes are not
  automatically validated by building `image/Dockerfile` and running
  `.github/scripts/compose-smoke-test.sh`. Preserve the default stack's internal
  ZAP API, persistent state and workspace mounts.
- Run focused tests while iterating, then the blank-line check and full suite
  for code changes. For documentation-only edits, verify referenced paths,
  commands and behavior, and run `git diff --check`; application tests are
  unnecessary unless executable behavior changed.
