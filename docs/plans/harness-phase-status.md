# Harness Implementation Status — Phases 1 & 2

**Plan reference:** [harness-plan.md](harness-plan.md) · [brief_explanation_of_harness.md](brief_explanation_of_harness.md)

## Phase 1 — Minimum viable driver (shipped)

Local/CTF target_class only; external is hard-blocked at driver startup until Phase 3.

| Area | Files |
|---|---|
| Driver subcommand `kerkov loop <run_dir>` | `src/harness/driver.py`, dispatched from `src/cli/main.py:141` |
| Harness parser (YAML frontmatter + body) | `src/harness/parser.py`, `src/harness/models.py` |
| Predicate DSL (`file_exists`, `line_count`, `line_match`, `content_match`, `AND` / `OR`) | `src/harness/predicates.py` |
| `task_list` mode end-to-end | parser + driver `_all_tasks_pass()` |
| Launch gate refusing `target_class: external` | `driver.driver_main()` |
| Per-iteration git snapshots inside the run dir (auto `git init`) | `_ensure_git_repo`, `_git_snapshot` |
| `state.json` round-trip + Pydantic validation | `src/harness/state.py`, `models.HarnessState` |
| `HarnessStateTool` — terminal tool, model self-report | `src/tools/harness_state_tool.py` |
| `KerkovCLI.run_harness_iteration()` — fresh subprocess entry point, autonomy 4 forced, harness context folded into base system prompt | `src/cli/kerkov_cli.py:252` |
| Iteration exit codes: 0 / 10 (abandoned) / 11 (budget) / ≥20 (crash) | `run_harness_iteration` |
| Coarse budgets: `max_iterations`, `max_wallclock_seconds` | driver `_loop` |
| Progress detection: new file under `artifacts/` | `_scan_artifacts` |
| `summary.md` written on every termination path | `_write_summary` |
| Reference template `ctf.md` | `harnesses/templates/ctf.md` |
| `harnesses/runs/` gitignored | `.gitignore:6` |
| `ctf` exposed in `--mode` argparse choices | `src/cli/main.py:45` |
| Tests: parser, predicates, driver helpers, `HarnessStateTool` | `tests/harness/` |

## Phase 2 — Goal-predicate mode, stuck detection, cost tracking (shipped)

### 2.1 — `goal_predicate` mode + stuck detection

| Change | Location |
|---|---|
| Typed `SuccessCriterion(id, description, check)` (was `List[dict]`) | `src/harness/models.py` |
| Parser validates `goal_predicate` requires ≥1 criterion; rejects duplicate task / criterion IDs | `src/harness/parser.py` |
| Driver `_all_predicates_pass()` dispatcher; `_all_success_criteria_pass()` parallel to `_all_tasks_pass()` | `src/harness/driver.py` |
| Stuck detection: `_is_stuck()` returns `DriverOutcome.STUCK` when `state.iteration - last_progress_iteration >= harness.stuck_after` | `src/harness/driver.py` |
| `summary.md` shows per-mode `[x] / [ ] / [?]` predicate checkboxes | `_write_summary` |
| `HarnessStateTool` adds `tasks_passed` + `forfeit_reason` params; cross-validates self-report IDs against the harness; requires `forfeit_reason` when status is `stuck` / `error` | `src/tools/harness_state_tool.py` |

### 2.2 — Cost tracking + token / USD budget enforcement

| Change | Location |
|---|---|
| Per-model pricing table with prefix matching for date-suffixed model IDs (Anthropic / Gemini / Ollama). Returns `None` for unknown models so cost enforcement is explicitly skippable. | `src/harness/pricing.py` (new) |
| `HarnessState` gains `cost_usd_this_iteration` + `cost_tracking_enabled` (sticky-false: once one iteration runs with unknown pricing, the cumulative cost number can't be trusted) | `src/harness/models.py` |
| Per-iteration token accumulators on `KerkovCLI` (`_iteration_prompt_tokens`, `_iteration_completion_tokens`); summed in `_record_usage` across every `complete()` call within an iteration | `src/cli/kerkov_cli.py` |
| `HarnessStateTool` drops the spoofable `tokens_used_this_iteration` param; constructor takes `usage_provider` lambda + `model_name`; reads prior `state.json` and merges cumulative `tokens_used_total` + `cost_usd_total` | `src/tools/harness_state_tool.py` |
| `exit.json` now writes prompt/completion split + active model name | `run_harness_iteration` |
| Driver `_check_budgets()` helper enforces `max_tokens` and `max_cost_usd` (cost only when tracking is enabled) | `src/harness/driver.py` |
| Startup banner + `summary.md` surface tokens / cost / budget config | `driver_main`, `_write_summary` |
| Parser drops Phase-1 "not enforced" warnings | `src/harness/parser.py` |

### Tests added in Phase 2

- `tests/harness/pricing_test.py` (new) — exact match, prefix match with date suffixes, longest-prefix-wins, unknown-returns-None, zero-token edge.
- `tests/harness/parser_test.py` — `goal_predicate` requires criteria, duplicate-ID rejection (tasks + criteria), token/cost budget fields parse without warnings.
- `tests/harness/driver_helpers_test.py` — `_all_success_criteria_pass`, mode-dispatch in `_all_predicates_pass`, `_is_stuck` (zero-iteration / within / at-threshold / recent-progress / disabled), `_check_budgets` (within / iteration / wallclock / token / cost / cost-disabled-when-untrusted), `HarnessStateTool` cumulative merging across iterations and sticky cost-tracking flag.
- `tests/cli/main_test.py` — updated to expect `harness_iteration` Namespace attr + `harness_iteration_dir` kwarg + `SystemExit(0)` from `main()`.

## Smoke test scaffold

- `harnesses/runs/smoke-ctf/harness.md` — picoCTF *amiable_citadel* crypto challenge wired up as a `task_list` smoke test (3 sequenced tasks, tight budgets, fast stuck-detection, picoCTF flag predicate). Run dir is gitignored.

## Other repo touch-ups in this work

- `src/cli/main.py:169` — `getattr(args, "harness_iteration", None)` so test mocks of `parse_args` don't have to enumerate every CLI flag.
- `.claude/CLAUDE.md:25` — async-test convention corrected from `asyncio.get_event_loop().run_until_complete()` (broken on Python 3.13) to `asyncio.run(coro)`, matching the rest of the test suite.

## Outstanding Phase 2 work

- **Anthropic prompt caching** — `cache_control: ephemeral` on the harness-context block in the system prompt. Pure cost optimisation; long `goal_predicate` runs are where cold-cache resends actually bite. No functional risk to deferring. Touches `src/providers/anthrophic_provider.py`.
- **External-targeting templates** — `harnesses/templates/external-recon.md` and `harnesses/templates/webapp-pentest.md`, both shipped with `target_class: external` so they remain unrunnable until Phase 3 lands the tool-layer scope rails.

## Phase 3 / 4 / 5 status

Untouched. Per the plan:

- **Phase 3** — tool-layer scope + rate-limit enforcement; flips the launch gate open for `target_class: external`. Hard prerequisite for any external-target run.
- **Phase 4** — `inbox/*.md` interjection, read-only `kerkov loop status`, polish.
- **Phase 5** — separate harness-generator utility (deferred, intentionally outside KerkovCLI).
