# Kerkov

Security-focused conversational AI CLI and local web console for authorized
penetration testing, security auditing, and CTF workflows. It drives LLM
providers (Anthropic, Gemini, NVIDIA NIM, OpenRouter, DeepSeek, Ollama, Codex)
through a shared tool-execution loop with operator approval.

## Working in this repository

- Treat the implementation, `pyproject.toml`, and the CI workflows as the source
  of truth for current behavior. This file, the design plans, and older docs can
  describe work that is incomplete or has since changed. When they disagree with
  the code, the code wins — and fix the stale text.
- Follow the requested scope. Extend the existing execution, approval,
  persistence, and proxy abstractions before adding parallel implementations.
- Preserve unrelated local changes and keep formatting limited to files you edit.
  Update relevant tests and user-facing docs when behavior changes.
- Do not attribute agent or AI involvement in commits, pull requests, issues,
  or review comments: no `Co-Authored-By` trailers for agents, no "Generated
  with …" footers, and no signatures. This overrides any default attribution
  your tool adds.
- This file is loaded into every session of every agent that reads it, and Codex
  stops reading at `project_doc_max_bytes` (32 KiB default, counted on disk, so a
  CRLF checkout is ~460 bytes larger). Keep it to rules and
  non-obvious invariants; mechanism and history belong in `docs/`.

## Repository map

Paths below are relative to the repository root.

| Path | Responsibility |
| --- | --- |
| `src/kerkov/cli/` | `main.py` entry point, `kerkov_cli.py` runtime, `input_parser.py`, terminal input/approval/agent-attach UI |
| `src/kerkov/core/session/` | Modes, autonomy, verbosity, approval mode and approval rules, session kernel/state |
| `src/kerkov/core/commands/` | `COMMAND_SPECS` registry shared by the CLI and web console |
| `src/kerkov/web/` | Django + Channels console; `runtime/` adapts the shared engine to WebSocket events, approvals and inventory |
| `src/kerkov/core/conversation_engine.py` | Shared loop for CLI, web sessions, and agents; `LoopSession`, `OutputSink`, `ApprovalStrategy` |
| `src/kerkov/providers/` | Provider implementations; factory and aliases in `__init__.py`, switching in `provider_manager.py` |
| `src/kerkov/tools/` | `BaseTool`, tool implementations, and `tool_suite.py` registration |
| `src/kerkov/agents/` | Profiles, `AGENT.md` loader, runner, budget registry, supervisor, detached sessions, delegation/report tools |
| `src/kerkov/routing/` | Delegate model policy, usage/cost measurement, and fixture-only evaluation |
| `src/kerkov/core/process/` | Process supervisor, session registry, POSIX/Windows PTY adapters, external-command runner, cleanup |
| `src/kerkov/core/compliance/` | Cinc Docker assessments, profile snapshots, evidence imports and outcome parsing |
| `src/kerkov/core/automation/` | Ansible invocation and immutable engagement profile snapshots |
| `src/kerkov/core/knowledge_base/` | SQLite/FTS5 artifact store, optional embeddings, and the structured findings ledger |
| `src/kerkov/core/engagement/` | `EngagementStore` transcript persistence and resume/restore |
| `src/kerkov/core/proxy/`, `core/intel/`, `core/websearch/`, `core/browser/` | ZAP adapter/runtime, CVE sources/cache, search backends, browser automation |
| `src/kerkov/core/` | Config, credentials/private files, approval scope/types, project paths, run workspaces, Docker plumbing, logging |
| `src/kerkov/context/`, `src/kerkov/models/` | Token tracking, compaction, skill loading; shared message/response models |
| `config/`, `src/kerkov/config/` | Project config and examples; packaged approval defaults and Ansible playbooks/callback plugin |
| `skills/`, `agents/` | Application `SKILL.md` skills and optional `AGENT.md` profiles |
| `image/`, `compose.yaml` | Container image/runtime smoke test and the local Kerkov + ZAP stack |
| `tests/` | Mirrors the `src/kerkov/` package layout |
| `docs/` | MkDocs site (`mkdocs.yml` nav): guides, reference, concepts, contributing; `background/` is unpublished |

## Implementation conventions

- Support Python `>=3.11,<3.15`; `.python-version` pins 3.13 for development and
  CI. Add type hints and follow nearby style. Both `Optional[T]` and `T | None`
  work on 3.11, so prefer the surrounding file's spelling. The configured mypy
  checks are in `pyproject.toml` (a set of explicit flags, not `strict = true`).
- Async everywhere: provider `complete()` and tool `run()`/`execute()` are
  async. Keep blocking SDK, network and process work off the shared event loop.
- Abstract interfaces use `abc.ABC` + `@abstractmethod`, not `typing.Protocol`.
  Providers extend `BaseLLMProvider`; tools extend `BaseTool`.
- Tools define Pydantic 2 `{ToolName}Params` with `Field` metadata and implement
  `get_params_class()`, `validate_params()`, `approval_scope()` and async
  `run()`, returning `ToolResult(success, output, error, metadata)`. The
  inherited `execute()` validates inputs and manages the execution call ID;
  preserve that wrapper.
- `approval_scope(params) -> ApprovalScope` (`core/approval_scope.py`) says how
  far one approval may be generalized when the operator saves it as a rule:
  `exact_only()`, `whole_tool()`, or a pattern from a boundary the tool's own
  arguments define (`matching`, `keeping`, `parent_directory`).
  `ApprovalScope(saveable=False)` disables saved interactive grants entirely,
  e.g. for mutable executable input such as Ansible playbooks. `volatile` names
  params that must never reach a saved rule — values the model varies per call
  (`timeout`) and payloads that would leak into the rules file
  (`write.content`, `http.headers`). It is abstract, not defaulted, so a new
  tool cannot inherit someone else's idea of "broad enough";
  `tests/tools/approval_scope_coverage_test.py` enforces that.
- Keep shared conversation behavior in `ConversationEngine`; route presentation
  and approval interactions through the sink/strategy interfaces. Check the CLI,
  web and agent callers when changing their shared contracts.
- Comments are sparse; write code that explains itself. Add a `#` comment only
  where something is genuinely non-obvious — a workaround, a non-local
  constraint, a subtle ordering requirement, a "why this and not the obvious
  thing". Never restate what the next line says; if code needs a comment to be
  followable, prefer a clearer name or a smaller function.
- Docstrings are one line. Expand only when a caller cannot use the thing
  correctly without it, and never into Args/Returns that repeat the type hints.
- Use Black (88 columns) and isort (Black profile). CI explicitly checks PEP 8
  blank lines with Flake8: two blank lines between top-level definitions, one
  between methods.
- The filename `src/kerkov/providers/anthrophic_provider.py` is an existing typo;
  do not rename it as incidental cleanup.

## Tool and provider wiring

- `ToolSuite.get_tools()` registers the built-in tools; read the list there
  rather than copying it here. `BrowserTool` is optional; the web runtime
  injects a connection-owned instance through `WebBrowser`, shared with that
  connection's agents. Keep its registry isolated and close it on engagement
  retirement and WebSocket disconnect. Agent mutations are rejected while the
  operator controls a page; direct operator actions bypass approval through
  `BrowserTool.run`.
- `WebSearchTool` registration is provider-aware: Tavily needs a configured key
  or an interactive credential manager/prompt; SearXNG needs `SEARCH_BASE_URL`.
  Search returns discovery snippets only — stage 2 is `ReadUrlTool` (`read_url`)
  for readable page text, or `HTTPTool` for raw responses and API requests.
  `/searxng setup` (`core/websearch/searxng_runtime.py`) provisions keyless
  SearXNG, including the `search.formats: json` entry a stock instance omits
  (without it every request gets 403). Setup/start/stop reload search tools for
  the CLI and future agents; running agents keep theirs.
- The CLI and web runtimes then add `SkillTool`, `SpawnAgentTool` (`delegate`)
  and `AgentWaitTool` (`agent_wait`), plus `RecallTool` and `LedgerTool` when
  `KB_ENABLED`. `recall` and `ledger` live at front-end level, not in
  `ToolSuite`, because they bind to the live `engagement_id` through late-bound
  getters. Check both runtimes when changing registration.
- Use `ExecTool` + `ProcessTool` for commands and ongoing sessions, and
  `ContainerLifecycleTool` for Docker Compose lifecycle/readiness. Do not
  reintroduce the deleted legacy shell stack (`shell_tool.py`, `core/shell/`).
- The supply-chain and SAST tools — `sbom_generate` (Syft/CycloneDX/SPDX),
  `vulnerability_scan` (Grype) and `sast_scan` (OpenGrep) — all require
  separately installed binaries on `PATH` and respect `EXECUTION_ENABLED`, and
  **never install or update software** (`kerkov setup opengrep` is the explicit
  path). `sast_scan` accepts absolute host-local paths; relative paths stay
  workspace-confined. Full reports/logs stay in the engagement workspace and
  bounded, redacted summaries go to the model. Built-in agent profiles do not
  receive `sast_scan`, because their whitelisted calls bypass approval.
- `ansible` runs trusted playbooks and snapshots engagement automation profiles
  under the shared root workspace. Profiles reference existing ledger resources;
  they are intended configuration, not permission boundaries. No built-in agent
  whitelists it. Check mode can mutate; keep approvals and execution gating.
- `compliance_scan` runs Cinc Auditor in a per-call owned Docker container via
  the existing Docker lifecycle helpers and shared command runner. It snapshots
  self-contained local profiles, mounts only explicit SSH files, requires
  host-key verification, and removes its container on completion, timeout or
  cancellation. It is execution-gated and in no built-in agent whitelist;
  profiles are executable code, so saved interactive grants are disabled.
- `compliance_report` imports existing Cinc/InSpec JSON without executing a
  scanner. Keep execution status separate from control outcomes; skipped,
  errored, empty and waived checks are not passes. Preserve raw evidence and
  bounded summaries; imports never attest host identity or change the ledger.
- Keep tool adapters thin: CVE intelligence belongs in `core/intel/`, search in
  `core/websearch/`, and ZAP integration behind the vendor-neutral
  `core/proxy/adapter.py`. Preserve process ownership and cancellation cleanup.
- Supported provider names are `anthropic`, `gemini`, `nvidia`, `openrouter`,
  `deepseek`, `ollama` and `codex`. Anthropic's alias is `claude`. Read the
  factory in `providers/__init__.py` for all aliases and `ConfigManager` for
  default models instead of copying model IDs into this guide.
- Codex auth is OAuth, started by provider setup; a missing sign-in at startup
  enters setup quietly, like a missing API key. Other providers resolve keys
  from config or the `CredentialManager`.
- Interactive provider selection saves the canonical name as `LLM_PROVIDER` in
  `~/.kerkov/settings.json`. Environment and `.env` win, then saved settings,
  then defaults; credentials stay separate. Use canonical names (`anthropic`,
  `gemini`) in model info and agent metadata.
- **Prompt-caching prefix stability (Anthropic).** `AnthropicProvider` emits two
  ephemeral cache breakpoints — one on the stable system+tools prelude, one
  rolling onto the last content block of the most recent turn (`_build_request`).
  A cache hit requires the prefix (tools, system, prior history) to be
  **byte-identical** across turns, so nothing volatile may live inside it: keep
  timestamps, engagement IDs, target hosts and per-turn interpolation out of the
  system prompt and tool schemas, and keep tool serialization deterministic
  (`_convert_to_anthropic_tool_schema` already builds from ordered dicts). Run
  `tests/providers/anthropic_caching_test.py` after touching prompt or
  tool-schema assembly.
- Mutable working state rides the tail. The findings ledger follows stable
  history as the final message for prefix-caching providers
  (`BaseLLMProvider.caches_prompt_prefix`, currently Anthropic and DeepSeek),
  after Anthropic's last cache breakpoint; other providers keep it at the front
  (`ConversationEngine._messages_for_provider`). That trades head-position
  salience for recency salience so ledger updates preserve the earlier prefix.
  Do not move volatile content back into the prefix.
- Cache-aware accounting: Anthropic's `usage.input_tokens` is only the uncached
  remainder, so `_usage_from_message` sets `prompt_tokens = input_tokens +
  cache_read_input_tokens + cache_creation_input_tokens` and recomputes
  `total_tokens` from it — `TokenTracker`/compaction must still see the full
  prompt size. DeepSeek already includes cache hits in `prompt_tokens`; report
  its cache hits separately without adding them again. **Never substitute a
  local byte/character token estimator** for authoritative `response.usage`.
- **Streaming assistant output.** `provider.complete()` takes an optional
  `on_delta: Callable[[str], None]`: when supplied the provider forwards text
  chunks as they arrive and still returns the **identical** final `LLMResponse`;
  when `None` it accumulates internally. The engine only threads a callback when
  `OutputSink.supports_streaming` is True (`ConsoleSink`, `BufferingSink`,
  `WebOutputSink`, per `STREAM_OUTPUT`; `QuietSink`/agents return False). The
  OpenAI-compatible providers reassemble SSE into the same response dict via
  `providers/openai_stream.py`.
- **Interrupt mid-stream.** `_complete` accumulates streamed chunks; on
  `InterruptedByUser` it attaches them as `partial_text` and `run_loop` records
  a `Message(role="assistant", partial=True)` so visible text is not lost.
  `Message.partial` persists through `EngagementStore`.

## Agents and delegation

- At the master level, `delegate` returns agent IDs immediately — agents run
  detached as supervised `asyncio.Task`s so the master keeps its prompt. Reports
  arrive through `agent_wait` or through supervisor delivery at the next turn
  boundary, **exactly once** (`AgentSession.report_delivered`). Both paths render
  through `agents/report_format.py` so the model sees one shape. Preserve that
  exactly-once property and the batch/iteration caps.
- Detachment is master-only. The **nested** `delegate` an agent receives is built
  with `detached=False` and blocks on its own children, because a detached child
  would bypass its parent: the parent would report without those findings while
  they landed in the master's. `agent_wait` is registered by the front end and
  never reaches the master-tools snapshot agents inherit.
- Agents carry an internal `id` (`a1`, monotonic, never reused within its
  supervisor — what `agent_wait` and logs key on) and a user-facing `name`
  (slugified and deduped per session). CLI output prints the name only;
  model-facing text carries `name (id, profile)`. Focus is keyed on the **id**,
  never the name, so a rename cannot move it.
- A task's `allowed_tools` may only **narrow** the selected profile's whitelist.
  Naming `report` there is tolerated (the runner appends `ReportTool` after
  validation, so no profile lists it), but `report` alone fails: it leaves no
  usable tools.
- Nested delegation is an exception to the whitelist: `_build_runner()` adds
  `delegate` when depth permits regardless of the profile's allowlist, and
  supplies the master's tool pool. Agents use `WhitelistAutoApproval`, so a
  restricted parent can spawn a more capable child without another approval.
  **Profile allowlists are not permission limits across descendants.**
- Agents get their own `TokenTracker` + `ContextCompactor` targeting their own
  window; otherwise a long run hits a provider context-limit error, which the
  master sees as `stuck` rather than partial findings. Agent spend is **not**
  folded into the master's `total_tokens` (its context size, which drives its
  compaction); billed spend goes to `cumulative_tokens`, and compaction bills
  through `TokenTracker.record_side_spend`. A terminating `report` breaks the
  loop *before* the post-turn compaction check, so a summarizer failure cannot
  replace a produced report with the `stuck` stub.
- Every delegate forks its provider, including in manual mode, so a running
  agent keeps its model after a master `/model` or `/provider` switch. `/model
  auto` and `AGENT_ROUTING_ENABLED` enable routing over three distinct
  configured model IDs; invalid startup settings warn and fall back to manual.
  `task_kind` is an eligibility hint — lower tiers additionally require bounded,
  benign work on the built-in `analysis` profile with effective tools limited to
  `read`/`recall`, and SMALL/MEDIUM agents cannot delegate further.
- `AgentRegistry` is the budget *policy* (depth/total, lifetime);
  `AgentSupervisor` owns live sessions, `AGENT_MAX_CONCURRENT`, cancellation and
  report delivery. It raises `ConcurrencyExceeded` (a `BudgetExceeded` subclass)
  so `delegate` keeps one error path while still distinguishing a full pool
  (clears itself) from a spent lifetime budget (never does).
- **Quit and switch paths must clean up.** `/q`, `Ctrl-C` and EOF must
  `await cancel_all()` or agents are orphaned; a `--message` one-shot instead
  waits, records the reports in the transcript and prints them, since there is no
  next turn. CLI `/clear`, `/resume` and `/mode` go through
  `settle_agents_for_conversation_switch()` (cancel, then
  `discard_pending_reports()`) because turn-boundary delivery fires against
  whatever conversation is live at the next turn — otherwise an in-flight
  agent's report is filed under a different engagement. Web `/clear` and `/mode`
  use `_reset_agents()` and retain the lifetime spawn budget.
- `/agents` lists active and finished sessions and attaches to one agent's
  read-only output view; `/back` returns to the master. Focus changes must not
  restart agent work. `/agents review <name|id>` opts one agent out of
  auto-approval, and only while the operator is attached to it — a detached agent
  has nobody to answer and would hang.
- **Master and all agents in a tree share one run workspace**, keyed on the root
  engagement id (same rule as the knowledge base) and injected by the supervisor;
  a per-agent directory would be invisible to the master and to siblings.
  Concurrent agents can therefore clobber files by name — the mitigation is a
  prompt convention to namespace outputs, not enforced isolation.
- `/engagements agents` lists sub-runs with parent linkage; `/resume` offers
  conversations only, and `/resume <agent-engagement-id>` opens that run
  **read-only** — an agent engagement's `mode="agent"` is not a `ModeType`, and
  continuing one would detach it from the `report` its parent awaits.
- See `docs/concepts/agent-subsystem.md` for lifecycle, attach/stop and
  operator-input mechanism, and `docs/reference/tools/delegate.md` for the tool
  surface.

## Modes, commands and approvals

- Operational modes are `RECON`, `AUDIT` (default), `OFFENSIVE` and `CTF`
  (`core/session/modes.py`). Startup `--mode` supports the first three;
  `/mode ctf` is available at runtime. `--mode` is terminal-CLI only; the web
  console picks its mode with `/mode`.
- Verbosity and approval mode are independent axes (`core/session/`).
  `/autonomy` and `AUTONOMY_LEVEL` apply legacy presets to both; level 4 and
  approval mode `yolo` require `ALLOW_AUTONOMOUS`.
- Approval is rules-based — there is **no** per-tool `NONE/OPTIONAL/REQUIRED`
  enum. Rules match first-wins in tier order: workspace (per-engagement),
  session (approved interactively this run), then file rules (user-level before
  project defaults). `strict` prompts unless the matching rule denies; `normal`
  honors rules and prompts on unmatched calls; `permissive` auto-approves
  unmatched calls; `yolo` bypasses rules and prompts. The CLI also has an
  explicit noninteractive auto-approval path in `ToolApprovalCliHelper`.
- When changing default rules, keep `config/approvals.yaml` and its packaged
  twin `src/kerkov/config/approvals.yaml` aligned — `default_approvals_source`
  falls back to the packaged copy outside a source checkout. Check both the CLI
  helper and `web/runtime/approval.py`.
- `core/commands/registry.py:COMMAND_SPECS` is the single command registry
  shared by the CLI and the web console; `cli/input_parser.py` pairs each spec
  with its CLI builder and handlers live in `cli/messages/`. Add or change
  commands in those places and avoid a second command list.
- Web main runs have a per-turn `EventInterrupt` and support
  `user.message.stop`. A racing completion is discarded with its usage recorded
  as side spend, except one carrying a stop-exempt `report`, whose findings still
  win. `/clear`, `clear` and `/mode` are not stoppable.

## Persistence, knowledge base and ledger

- `EngagementStore` (`core/engagement/`) persists transcripts as JSONL under
  `~/.kerkov/engagements/`. Preserve metadata compatibility and
  provider-specific raw message blocks on save/resume. `--continue` resumes the
  latest conversation; `--resume <id>` selects one. Resume preserves the active
  provider/model, validates saved settings and history against the model context
  window minus output reserve *before* retiring the current session, and guards
  ownership so two browser sessions cannot write the same engagement.
- `/usage [engagement-id]` summarizes an engagement tree from append-only
  `usage/<engagement-id>.jsonl` sidecars. `override_engagement_messages` and
  compaction must leave them intact; `delete_engagement` removes them. Missing
  starts, unreadable events or recorded gaps mean incomplete history, and
  unavailable usage or cost stays **unknown** — never zero. Optional
  `MODEL_PRICING_FILE` rates use canonical
  provider/model keys. Cache tokens are already in prompt totals and Gemini
  thinking counts as output, so do not double-count.
- `RunWorkspaceManager` manages `~/.kerkov/workspaces/<root-engagement-id>/`
  (`WORKSPACE_DIR` overrides the root). Use its context-local helpers and path
  validation when touching artifacts from concurrent runs.
- `EngagementKnowledgeBase` stores artifacts in `<root-engagement-id>.kb.sqlite`
  alongside the transcripts. **One store per engagement *tree***: an agent has
  its own `engagement_id`, so a per-id file would be a private database its
  master could never read. `engagement_id` is a column and spawn passes the
  master's store down. `recall` searches the whole tree by default (optional
  `engagement` filter), so a master can drill into a delegate's raw output.
- `ConversationEngine` indexes the original tool output before normalizing it
  for history — in every front end and for agents, including when the KB is off
  or ingestion fails — and the output sink always receives the original result.
  The JSONL stores the **normalized** result, so with the KB off, omitted raw
  output is unrecoverable. Results above `KB_CLIP_THRESHOLD_BYTES` (output *and*
  metadata, since both render) gain an `artifact#N` reference and have oversized
  metadata elided. `recall` is exempt so an expanded artifact is complete, and
  `read_url` pages stay inline under `READ_MAX_CHARS` via `KB_CLIP_EXEMPT_TOOLS`,
  because a second normalization would hide part of the head before
  `next_offset`.
- `recall(source=...)` matches a substring against the tool name *and* the
  artifact title (`http: <url>`, `read: <path>`); the source column holds only
  the tool name, so a host or URL filter would otherwise return nothing.
  `ArtifactKind` holds only kinds something produces, since an unreachable
  member is a `recall(kind=...)` filter that is always empty. `credential` and
  `note` are deliberately absent — those facts belong in the ledger.
- `FindingsLedger` (`core/knowledge_base/findings_ledger.py`) is the model's
  structured working memory — assets, targets, connections, containers, hosts,
  services, credentials, findings, flags, next steps — written through the
  `ledger` tool and **re-injected every turn**, so it survives compaction
  verbatim. It lives in the same `.kb.sqlite` with the same root-keying,
  concurrency and lifecycle. Injection is transient — never written into
  `history`, so `ContextCompactor` never sees it. Since compaction cannot
  reclaim it, `serialize()` trims to `max_injected_bytes` by dropping whole
  lowest-priority sections (credentials, flags and findings outlive host and
  service enumeration) and notes what it omitted; `ledger op=list` still reads
  everything.
- Ledger identity is required, not guessed: `identity_error()` rejects an `add`
  that cannot be identified, because a hash-fallback row never merges with its
  own later enrichment and host-less services would all collapse onto one port.
  Identity firms up over time, so `_identity()` also returns alias keys and
  `add_ledger_entry` folds duplicates into the oldest row. For ordinary merges
  the first contributor keeps `engagement_id` ownership —
  `delete_engagement_ledger_rows` deletes by owner, so reassigning it would let
  an enriching agent's cleanup take the master's facts. DNS and deployment
  relationships are never inferred; connections follow the typed assertion rules
  in `docs/concepts/findings-ledger.md`.
- The knowledge base and ledger are **plaintext on local disk and hold whatever
  the tools returned, harvested credentials included**. Nothing is redacted,
  nothing is masked on re-injection, and nothing leaves the machine. Treat them
  as working memory, not an audit log.
- Both front ends wire the ledger beside the knowledge base and pass `ledger=`
  into their `LoopSession`: the mode prompt tells the model to record facts, so a
  front end without the tool would advertise something it does not have. The
  ledger shares `KB_ENABLED`; there is no separate flag.
- `WebInventory` (`web/runtime/inventory.py`) is the browser's view of the same
  ledger. Resource roles are engagement context, **not** tool permissions, and
  resource rows always belong to the root engagement, including agent writes.
- **Engagement retirement ordering** (web `/clear`, `/mode`, and Engagement-tab
  new/resume): detach KB, ledger and workspace; reset inventory, scripts and
  browser contexts; settle agents and drain in-flight inventory/scripts reads;
  then release the engagement claim, rotate its context and
  `engagement_store.detach()`. In `finally`, drain the browser action/preview
  lock and close browser sessions, even if agent reset fails or is cancelled;
  close the detached KB in a nested `finally` so browser cleanup failure cannot
  skip it. This keeps stale responses out of the new engagement and avoids
  closing storage while an old request still uses it. See
  `docs/concepts/web-architecture.md`.
- `core/config_manager.py` and `.env.example` document every setting, and
  `docs/reference/configuration.md` is the user-facing reference. Read defaults
  there rather than copying them into this file. `CredentialManager` handles API
  secrets through masked prompts, session storage, OS keyring or a protected
  fallback file — reuse the credential, private-file and subprocess-environment
  helpers, and keep provider/tool keys out of chat, logs and ordinary settings.
- Codex static configuration is copied from `config/codex.example.json` to
  `config/codex.json`; OAuth state defaults to `data/providers/codex/auth.json`
  and non-secret runtime settings to `~/.kerkov/settings.json`. Keep generated
  state and secrets out of commits.
- The web console is loopback-only and has no user-account authentication.
  Preserve its host/origin/access-token checks and session cleanup. It consumes
  existing/environment credentials and does not expose the CLI's masked
  credential-entry flow.

## Context and compaction

- `ContextCompactor` runs when `TokenTracker.total_tokens` crosses
  `COMPACTION_THRESHOLD_PCT` of `MAX_CONTEXT_TOKENS` — reactive to authoritative
  `response.usage` only. There is deliberately **no local byte/char token
  estimator**; do not add one. One compaction runs in a fixed order: evict large
  tool results, summarize the remaining narrative, then roll summaries up.
- Provenance is persisted, not in-memory: `Message.summary_level` and
  `Message.artifact_ref` are threaded through `EngagementEntry` and both
  `EngagementStore` write paths, because `_check_compaction` rewrites the JSONL
  with the compacted history — an in-memory mark would be lost on `/resume`, and
  the next compaction would re-summarize summaries.
- Selection fails **loudly**: a `None` return logs and warns once rather than
  passing for a no-op success. The recent window is bounded by both
  `PRESERVE_RECENT_TURNS` and `PRESERVE_RECENT_BYTES`, so one oversized recent
  result cannot pin it beyond eviction's reach. A `tool_use`/`tool_result` group
  is never split.
- Eviction replaces **only the output body**, cutting at `TOOL_OUTPUT_MARKER`
  (which is why `format_tool_result` renders output last): the header carries the
  tool name, status, error and metadata, which exist *only* in the conversation
  message, while the artifact holds `ToolResult.output` alone. Artifact handles
  are carried **deterministically**, not by prompt: the summary is model text and
  the messages holding each `artifact#N` are dropped, so `build_artifact_index()`
  appends every id in the range verbatim, and rollup does the same.
- Rollup asks for a *merge*, not a further compression, and its second bounded
  pass (level-2s among themselves) is not optional: summaries are excluded from
  every compactable range, so without it level-2s accumulate until the threshold
  is crossed with nothing left to compact.
- `docs/concepts/context-and-compaction.md` and
  `docs/concepts/knowledge-base.md` have the full mechanism.

## Development and verification

```bash
uv sync --locked --extra dev --extra test --extra web
uv run kerkov --help
uv run kerkov
uv run --no-sync kerkov-web runserver
uv run python -m pytest tests/tools/tool_suite_test.py  # Example focused test
uv run python -m flake8 --select=E301,E302,E303,E305,E306 src tests
uv run python -m pytest tests                           # Full CI test command
```

- Include the `web` extra for the complete test suite; CLI-only installs can omit
  it. Keep `uv.lock` consistent with intentional dependency changes.
- Pytest runs both unittest-style cases and pytest functions. Test files are
  `*_test.py` (never `test_*.py`), functions `test_*`, and pytest-collected
  classes `*Test`. Follow the surrounding test style.
- For async tests outside `tests/web/`, use an explicit `asyncio.run()` wrapper.
  Pytest manages the loop for native async web tests (`@pytest.mark.asyncio`).
  Do not assume `asyncio.get_event_loop()` will create a loop.
- Use `unittest.mock`/`AsyncMock` or pytest fixtures for external services, and
  define `_create_mock_*()` factories at module level. Isolate filesystem and
  configuration state with temporary paths and in-memory credential stores
  rather than the developer's real `~/.kerkov` or keyring.
- Format edited Python files with `uv run black <paths>` and
  `uv run isort <paths>`. `uv run mypy src/` and `uv run bandit -r src/` are
  available but are not CI gates; the Flake8 check and full pytest run are.
- Container build CI is disabled
  (`.github/workflows/build-kerkov.yml.disabled`), so nothing builds
  `image/Dockerfile` or runs `.github/scripts/compose-smoke-test.sh`
  automatically. Preserve the default stack's internal ZAP API, persistent state
  and workspace mounts.
- Run focused tests while iterating, then the blank-line check and the full suite
  for code changes. For documentation-only edits, verify referenced paths,
  commands and behavior and run `git diff --check`; skip application tests.
