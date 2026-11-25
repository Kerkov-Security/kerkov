# Kerkov

Security-focused conversational AI CLI and local web console for authorized
penetration testing, security auditing and CTF workflows. Providers share a
tool-execution loop with operator approval.

## Working in this repository

- Treat the implementation, `pyproject.toml`, and the CI workflows as the source
  of truth for current behavior. This file, the design plans, and older docs can
  describe work that is incomplete or has since changed. When they disagree with
  the code, the code wins — and fix the stale text.
- Follow the requested scope. Extend the existing execution, approval,
  persistence, and proxy abstractions before adding parallel implementations.
- Preserve unrelated local changes and keep formatting limited to files you edit.
  Update relevant tests and user-facing docs when behavior changes.
- No agent/AI attribution in commits, PRs, issues or review comments: no agent
  `Co-Authored-By`, "Generated with …" footers or signatures, including tool defaults.
- Codex truncates this file at `project_doc_max_bytes` (32 KiB on disk by default;
  allow for CRLF). Keep rules and invariants here; mechanism and history in `docs/`.

## Repository map

| Path | Responsibility |
| --- | --- |
| `src/kerkov/cli/` | `main.py`, `kerkov_cli.py`, `input_parser.py`; terminal UI |
| `src/kerkov/core/session/` | Modes, approvals and session state |
| `src/kerkov/core/commands/` | `COMMAND_SPECS` registry shared by the CLI and web console |
| `src/kerkov/web/` | Django/Channels console; `runtime/` adapts the shared engine |
| `src/kerkov/core/conversation_engine.py` | Shared loop; `LoopSession`, `OutputSink`, `ApprovalStrategy` |
| `src/kerkov/providers/` | `openai_compatible.py` base, `__init__.py` factory, `provider_manager.py` switching |
| `src/kerkov/tools/` | `BaseTool`, tool implementations, and `tool_suite.py` registration |
| `src/kerkov/agents/` | Profiles, loader, runner, budgets, supervisor, delegation/reports |
| `src/kerkov/routing/` | Delegate model policy, usage/cost measurement, and fixture-only evaluation |
| `src/kerkov/core/process/` | Supervisor, session registry, POSIX/Windows PTY adapters, command runner |
| `src/kerkov/core/compliance/` | Cinc Docker assessments, profile snapshots, evidence imports and outcome parsing |
| `src/kerkov/core/automation/` | Ansible invocation and immutable engagement profile snapshots |
| `src/kerkov/core/knowledge_base/` | SQLite/FTS5 artifacts, embeddings and findings ledger |
| `src/kerkov/core/engagement/` | `EngagementStore` transcript persistence and resume/restore |
| `src/kerkov/core/proxy/`, `core/intel/`, `core/websearch/`, `core/browser/` | ZAP adapter/runtime, CVE sources/cache, search backends, browser automation |
| `src/kerkov/core/` | Config, credentials, approvals, paths, workspaces, Docker and logs |
| `src/kerkov/context/`, `src/kerkov/models/` | Tokens, compaction, skills and message models |
| `config/`, `src/kerkov/config/` | Config, approvals and Ansible playbooks/callback |
| `skills/`, `agents/` | Application `SKILL.md` skills and optional `AGENT.md` profiles |
| `image/`, `compose.yaml` | Container image/runtime smoke test and the local Kerkov + ZAP stack |
| `tests/` | Mirrors the `src/kerkov/` package layout |
| `docs/` | MkDocs (`mkdocs.yml`); `background/` is unpublished |

## Implementation conventions

- Support Python `>=3.11.5,<3.15`; development/CI use 3.13 (`.python-version`).
  Add type hints; follow nearby style, including `Optional[T]` versus `T | None`.
  Mypy uses explicit `pyproject.toml` flags, not `strict = true`.
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
- Prefer clear names and small functions to explanatory comments. Use `#` only
  for non-obvious workarounds, non-local constraints, ordering or rationale;
  never restate the next line.
- Docstrings are one line unless correct use requires more. Never repeat type
  hints in Args/Returns.
- Use Black (88 columns), isort (Black profile), and Flake8's PEP 8 blank lines:
  two between top-level definitions, one between methods.
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
  `bootstrap_python=true` explicitly enables packaged raw-SSH installation of
  missing Python 3 on Debian/Ubuntu/Alpine before the selected playbook. Preview only
  reports the prerequisite; bootstrap must not install packages in check mode.
  `builtin:openscap_assess` requires check=false and verifies the controller
  datastream digest before upload. `builtin:openscap_install` installs scanner
  packages with privilege escalation only when check=false; check mode reports
  whether the scanner is present without installing or running verification.
- `compliance_scan` snapshots local profiles with locked, vendored dependencies.
  Its owned Cinc container uses explicit SSH files and host-key verification
  and is removed on completion, timeout or cancellation. Scans and project
  preparation require `EXECUTION_ENABLED`, disable saved grants
  (`saveable=False`), and are absent from built-in agent whitelists.
- OpenSCAP uses the shared Ansible executor and explicit SSH files, scans a
  staged and hashed datastream, and provisions through a separate `ansible`
  call. Both tool and runtime enforce execution gating. Final controller
  manifests record completion, failure or cancellation; interrupted Ansible
  may leave remote work or temporary files behind.
- `compliance_profile` validates GitHub-only sources and pinned dependencies.
  Preparation requires the reviewed commit, downloads through GitHub, and runs
  Cinc offline. The global `COMPLIANCE_PROJECTS_DIR` cache lives outside the root
  workspace. Refresh publishes atomically and prunes old generations under lock.
- `compliance_report` imports Cinc/InSpec JSON or OpenSCAP XML offline. Keep
  execution status separate from control outcomes. Skipped, errored, empty and
  waived checks do not pass. Preserve raw evidence and bounded summaries.
  Imports never attest host identity or change the ledger.
- MCP tools (`mcp__<server>__<tool>`, `core/mcp/` + `tools/mcp/`): freeze and
  sort them per connection, never trust cwd `mcp.json`/`.env`, keep
  `prompts_every_call` (never given to agents) unless `trusted`. One
  `McpManager` per CLI/web socket: switches cancel calls, quit/disconnect
  closes it. See `docs/concepts/mcp-client.md`.
- Keep tool adapters thin: CVE intelligence belongs in `core/intel/`, search in
  `core/websearch/`, and ZAP integration behind the vendor-neutral
  `core/proxy/adapter.py`. Preserve process ownership and cancellation cleanup.
- Supported provider names are `anthropic`, `gemini`, `nvidia`, `openrouter`,
  `deepseek`, `ollama` and `codex`. Anthropic's alias is `claude`. Read the
  factory in `providers/__init__.py` for all aliases and `ConfigManager` for
  default models instead of copying model IDs into this guide.
- CLI setup and web Manage providers share Codex OAuth. Missing sign-in enters
  CLI setup quietly; other providers resolve keys from config or `CredentialManager`.
- Interactive provider selection saves the canonical name as `LLM_PROVIDER` in
  `~/.kerkov/settings.json`. Environment and `.env` win, then saved settings,
  then defaults; credentials stay separate. Use canonical names (`anthropic`,
  `gemini`) in model info and agent metadata.
- Anthropic caching requires byte-identical tools, system and prior history.
  Keep timestamps, engagement IDs, targets and per-turn interpolation out of the
  system prompt and tool schemas; serialize tools deterministically. Preserve
  both ephemeral breakpoints (stable prelude and last block of the recent turn).
  Run `tests/providers/anthropic_caching_test.py` after prompt/schema changes.
- Mutable ledger state follows stable history for
  `BaseLLMProvider.caches_prompt_prefix` providers (Anthropic and DeepSeek), after
  Anthropic's last breakpoint; others keep it at the front. Preserve this ordering
  in `ConversationEngine._messages_for_provider`.
- Anthropic prompt totals include uncached input plus cache reads and creations;
  DeepSeek prompt totals already include cache hits. Do not double-count or
  substitute local token estimates for authoritative `response.usage`.
- `provider.complete(on_delta=...)` must return the same final `LLMResponse` as
  non-streaming completion. Only sinks with `supports_streaming` receive deltas;
  agents remain non-streaming. Preserve SSE reassembly in `openai_stream.py`.
- An interrupted stream keeps its visible text: `run_loop` records
  `partial_text` as `Message(role="assistant", partial=True)`, persisted.

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
  `delegate` when depth permits regardless of the profile's allowlist. Its pool
  is the **parent agent's own tools**, never the master's, so a child gets its
  profile intersected with what its parent holds and a profile allowlist caps
  the whole subtree. Agents use `SupervisedAgentApproval`, so a wider pool
  reaching a child would bypass approval; nested validation rejects requests
  naming tools the parent lacks or profiles sharing none of its tools.
- Agents honour the operator's **deny rules** (`AgentDenyRules`: the front
  end's live rules engine and approval mode, skipped only in `yolo`) before
  whitelist auto-approval; allow rules and unmatched calls do not affect them.
  Both front ends must pass `deny_rules=` to the master `SpawnAgentTool`.
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

- Operational modes are security-team roles: `RED`, `PURPLE` (default) and
  `BLUE` (`core/session/modes.py`); the internal enum is still `ModeType`. There
  are no legacy mode aliases — the pre-refactor names (`recon`, `audit`,
  `offensive`, `ctf`) are rejected by `--mode`/`/mode`, and a saved engagement
  that recorded one fails to restore loudly (`EngagementRestoreError`) rather
  than being silently remapped. `--mode` is terminal-CLI only; the web console
  picks its mode with `/mode`.
- Verbosity and approval mode are independent axes (`core/session/`).
  `/autonomy` and `AUTONOMY_LEVEL` apply legacy presets to both. Level 4 and
  approval mode `yolo` are available without an additional opt-in setting.
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
- `core/workflows/`: both front ends read `WORKFLOW_DEFINITIONS` (role, static
  overlay sent as a transient prompt); CTF's active state is
  `EngagementStore.workflow`, never front-end state. `ComplianceRun` gates plan
  calls before shared approvals via a `WorkflowContext` of live getters; never
  snapshot them.
  Denial, changed files or storage failure block runs; records live in
  `automation/compliance-runs/`. Web `compliance.run` shares admission, turn lock
  and Stop; CLI `/workflow` refuses `--message`. Keep one approval path.
- Web `compliance.project.prepare` shares admission, turn lock, Stop and tool
  approval. It executes one `compliance_profile` call with the reviewed commit
  and routes output through `compliance.event` outside the transcript and KB.

## Persistence, knowledge base and ledger

- `EngagementStore` (`core/engagement/`) persists transcripts as JSONL under
  `~/.kerkov/engagements/`. Preserve metadata compatibility and
  provider-specific raw message blocks on save/resume. `--continue` resumes the
  latest conversation; `--resume <id>` selects one. Resume preserves the active
  provider/model, validates saved settings and history against the model context
  window minus output reserve *before* retiring the current session, and guards
  ownership so two browser sessions cannot write the same engagement.
- Loading repairs dangling tool calls with synthetic results whose execution
  status is UNKNOWN. Never rerun a call or infer failure from missing output;
  execution may have changed the target. Preserve real results, provider replay
  blocks and `context_only`; loading does not rewrite the original transcript.
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
  reclaim it, `serialize()` trims to `max_injected_bytes` by dropping entries
  from the lowest-priority sections first, oldest first within a section
  (credentials, flags and findings outlive host and service enumeration) and
  notes what it omitted; `ledger op=list` still reads
  everything.
- Ledger identity is required, not guessed: `identity_error()` rejects an `add`
  that cannot be identified, because a hash-fallback row never merges with its
  own later enrichment and host-less services would all collapse onto one port.
  Identity firms up over time, so `_identity()` also returns alias keys and
  `add_ledger_entry` folds duplicates into the oldest row, whose `engagement_id`
  records the first contributor. Deleting an agent sub-run never removes ledger
  rows: the master may have enriched or acted on what the agent recorded. DNS
  and deployment relationships are never inferred; connections follow the
  typed assertion rules in `docs/concepts/findings-ledger.md`.
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
  browser contexts; cancel MCP calls; settle agents and drain in-flight
  inventory/scripts reads; then release the engagement claim, rotate its
  context and `engagement_store.detach()`. In `finally`, drain the browser
  action/preview lock and close browser sessions, even if agent reset fails or
  is cancelled; close the detached KB in a nested `finally` so browser cleanup
  failure cannot skip it. See `docs/concepts/web-architecture.md`.
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
- The web console has no user accounts: preserve loopback, host/origin/token
  checks and cleanup. OAuth tokens stay on the server; the browser receives only
  the authorize URL. See `docs/concepts/web-architecture.md` for coordination.

## Context and compaction

- `ContextCompactor` runs when `TokenTracker.total_tokens` crosses
  `COMPACTION_THRESHOLD_PCT` of `MAX_CONTEXT_TOKENS` — reactive to authoritative
  `response.usage` only. There is deliberately **no local byte/char token
  estimator**; do not add one. One compaction runs in a fixed order: evict large
  tool results, summarize the remaining narrative, then roll summaries up.
- Provenance is persisted: `Message.summary_level`, `Message.artifact_ref` and
  `Message.context_only` are threaded through `EngagementEntry` and both
  `EngagementStore` write paths, because `_check_compaction` rewrites the JSONL
  with the compacted history — an in-memory mark would be lost on `/resume`, and
  the next compaction would re-summarize summaries.
- `context_only` hides workflow messages from web chat, never from the model.
  Summarization and both rollup levels keep visible and hidden messages in
  separate groups (at most two outputs per pass), with separate artifact indexes.
  A workflow must neither hide visible chat nor leak into its visible summary.
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
uv run python -m pytest tests/tools/tool_suite_test.py
uv run python -m flake8 --select=E301,E302,E303,E305,E306 src tests
uv run python -m pytest tests -n auto
```

- Full tests require the `web` extra; CLI installs can omit it. Update `uv.lock`
  for intentional dependency changes.
- Pytest runs unittest cases and pytest functions. Use files `*_test.py` (never
  `test_*.py`), functions `test_*`, classes `*Test`; follow nearby style.
- For async tests outside `tests/web/`, use an explicit `asyncio.run()` wrapper.
  Pytest manages the loop for native async web tests (`@pytest.mark.asyncio`).
  Do not assume `asyncio.get_event_loop()` will create a loop.
- Use `unittest.mock`/`AsyncMock` or pytest fixtures for external services, and
  define `_create_mock_*()` factories at module level. Isolate filesystem and
  configuration state with temporary paths and in-memory credential stores
  rather than the developer's real `~/.kerkov` or keyring.
- Format edited Python with `uv run black <paths>` and `uv run isort <paths>`.
  Flake8 and full pytest are CI gates; `uv run mypy src/` and
  `uv run bandit -r src/` are optional.
- Container build CI is disabled
  (`.github/workflows/build-kerkov.yml.disabled`), so nothing builds
  `image/Dockerfile` or runs `.github/scripts/compose-smoke-test.sh`
  automatically. Preserve the default stack's internal ZAP API, persistent state
  and workspace mounts.
- Run focused tests while iterating, then the blank-line check and the full suite
  for code changes. For documentation-only edits, verify referenced paths,
  commands and behavior and run `git diff --check`; skip application tests.
