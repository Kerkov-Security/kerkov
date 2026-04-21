# Harness Feature — Design Plan

**Status:** Draft

## Goal

Allow a user to define a cybersecurity objective (CTF, recon, exploit development, vulnerability research) and have Kerkov run autonomously — in a loop of fresh CLI instances — until the objective's success criteria are met, a budget is exhausted, the run goes stuck, or the user interrupts.

## Non-goals (v1)

- In-process session compaction as the loop mechanism. Each iteration is a fresh subprocess; the existing compaction feature (PR #72) is unrelated.
- Authoring harnesses via a second LLM ("target generator"). Users write harnesses by hand or copy from templates. Generator may come later.
- Full multi-agent orchestration. One harness, one run, one driver, sequential iterations.

## Terminology

| Term | Meaning |
|---|---|
| **Harness** | A declarative spec: goal, scope, success criteria, budgets. Lives as `harness.md` + frontmatter. |
| **Template** | A reusable harness skeleton (e.g., `ctf.md`, `external-recon.md`) users copy and fill in. |
| **Run** | One execution of a harness. Lives in `./harnesses/runs/<slug>/`. |
| **Iteration** | One invocation of Kerkov within a run. Spawned as a fresh subprocess. |
| **Driver** | Loop controller. Spawns iterations, enforces budgets, checks termination. Does **not** call any LLM. |

Naming chosen: `harness` for the spec, `run` for an instance. Avoids overloading "target" (already means the system under test).

## Architecture

```
┌────────────────────────────────────────────────────┐
│  kerkov loop ./harnesses/runs/acme-ctf/              │  <- driver process
│  ┌──────────────────────────────────────────────┐  │
│  │ 1. read harness.md + state.json               │  │
│  │ 2. enforce budgets (hard kill on breach)      │  │
│  │ 3. check terminal conditions                  │  │
│  │ 4. drain inbox/*.md into iteration prompt     │  │
│  │ 5. spawn fresh Kerkov subprocess  ─────┐      │  │
│  │ 6. wait for exit, parse state.json     │      │  │
│  │ 7. git commit run dir (optional)       │      │  │
│  │ 8. loop                                │      │  │
│  └────────────────────────────────────────┼──────┘  │
│                                           ▼         │
│  ┌──────────────────────────────────────────────┐  │
│  │ python -m src.cli.main                       │  │
│  │   --harness-iteration <run_dir>               │  │
│  │   (fresh instance, clean context)             │  │
│  │   reads harness.md, progress.txt, findings.md│  │
│  │   tool-loops until HarnessStateTool is called │  │
│  │   (or budget hit), writes exit.json, exits    │  │
│  └──────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────┘
```

**Separation of concerns:**

- **Driver** is cheap, deterministic, killable. No LLM calls. Python `asyncio` or synchronous — doesn't matter, it only spawns subprocesses and reads JSON.
- **Kerkov subprocess** does one iteration of work. It does not know about the loop. It reads state, does work, writes state, exits.

This matches the ralph pattern (snarktank/ralph) — fresh instance per iteration eliminates context pollution without needing in-process compaction.

## Existing code to build on

Reuse first — several pieces of the feasibility story already exist:

| Need | Existing anchor | Notes |
|---|---|---|
| Tool-loop engine for a single iteration | `src/cli/kerkov_cli.py` — the `UserMessage.execute()` path invoked from `run_non_interactive()` at `kerkov_cli.py:231-245` | The *engine* we reuse is the model-driven tool loop that `UserMessage.execute()` runs, not `run_non_interactive()` wholesale. `run_non_interactive()` is designed to process a scripted `--message` list and exit — its termination contract ("exit when inputs drain") is wrong for a harness iteration ("exit when `HarnessStateTool` is called, or budget hit"). Phase 1 adds a sibling method `run_harness_iteration()` that reuses the same tool loop with harness-appropriate setup and termination detection. Non-interactive mode stays as-is for CI / one-shot scripting (reference: `scripts/ci/run_vulhub_e2e.py:130-141`). |
| Tool auto-approval in subprocess | `src/cli/kerkov_cli.py:236`, `src/cli/tool_approval_helper.py:36-80`, `src/cli/autonomy_manager.py:17-31,63,103` | Non-interactive already sets `non_interactive_auto_approve=True`. Autonomy level 4 (AUTONOMOUS) + `ALLOW_AUTONOMOUS=true` env var is the supported fully-autonomous path — driver sets these in the subprocess env, no new approval mode needed. |
| Injecting harness context files | `src/cli/kerkov_cli.py:504-513` (base system prompt assembly in `_apply_system_prompt_if_possible`) | Fold harness context into the base system prompt, **not** `_transient_system_prompts`. Two reasons: (a) `_messages_for_provider()` appends transient prompts *after* conversation history (line 528-532), so by tool turn 8 the harness framing is buried behind tool output; (b) transient was designed for the one-shot `NON_INTERACTIVE_EXECUTION_PROMPT` nudge and uses positional `pop()` (line 245) — fine for a single pusher, fragile if harness and driver both push. Each fresh subprocess starts with empty `conversation_history`, so extending the base prompt path runs once per iteration, exactly where it belongs. |
| Iteration transcripts | `src/cli/engagement_store.py` (full JSONL message log, including `override_engagement_messages()` for compaction) | Prefer reusing the engagement store for per-iteration transcripts instead of reinventing `iterations/NNNN/transcript.jsonl`. A run can assign one engagement per iteration, or one engagement per run with boundary markers. |
| Token counts | `src/models/basic_models.py:4-8` (`TokenUsage`), `src/context/token_tracker.py` | Token accounting exists per call; cumulative cost in USD does not (see open question 3). |

What's genuinely missing (new code required):

- Per-provider **USD pricing tables** and a `CostTracker` aggregating across iterations.
- Tool-layer **scope/denylist/rate-limit** plumbing — `FetchTool` accepts any URL; shell tools have only a binary `sandbox_mode`, no regex denylist. The config flag `REQUIRE_SUDO_APPROVAL` exists (`src/core/config_manager.py:50`) but is not wired into tool execution — worth wiring while we're there.
- Structured per-iteration **exit state**. The model should not be trusted to hand-write JSON to a well-known path; a built-in `HarnessStateTool` (or equivalent post-run hook) writes `state.json`.
- Driver binary / subcommand and the harness/predicate parser.

## Directory layout

```
./harnesses/
├── templates/                          # committed to repo, reusable
│   ├── ctf.md
│   ├── external-recon.md
│   ├── webapp-pentest.md
│   └── binary-exploit-dev.md
└── runs/                               # gitignored or separately tracked
    └── 2026-04-19-acme-ctf/
        ├── harness.md                  # goal, scope, success criteria, budgets
        ├── progress.txt                # append-only learnings log
        ├── findings.md                 # structured discoveries
        ├── dead_ends.md                # approaches that failed — don't re-try
        ├── AGENTS.md                   # institutional knowledge for this run
        ├── state.json                  # driver <-> kerkov contract (written by HarnessStateTool)
        ├── inbox/                      # optional: queued user interjections (*.md consumed FIFO)
        ├── artifacts/                  # PoCs, scripts, evidence
        └── iterations/
            └── 0012/
                ├── prompt.txt          # full prompt sent to Kerkov
                ├── transcript.jsonl    # message log for audit (reuse engagement_store)
                └── exit.json           # exit code, wallclock, tokens used
```

## Harness schema

Harnesses are markdown files with YAML frontmatter. Two modes share a common envelope.

### Shared fields (required for both modes)

```yaml
---
name: acme-ctf-web-challenge
description: Web exploitation CTF challenge from ACME qualifier 2026
mode: task_list | goal_predicate

target_class: local | ctf | external   # launch gate — see below

scope:
  in_scope:
    - https://ctf.acme.example/challenge-7/*
  out_of_scope:
    - any host not matching in_scope
    - DoS patterns
    - lateral movement attempts

budget:
  max_iterations: 50                 # Phase 1
  max_wallclock_seconds: 14400       # Phase 1 — 4 hours
  max_tokens: 5_000_000              # Phase 2 (requires CostTracker)
  max_cost_usd: 25.00                # Phase 2 (requires per-provider pricing tables)

allowed_tools: [shell, read, write, fetch, crypto]
forbidden_actions:
  - brute-force auth (>10 attempts/minute)
  - writes outside ./artifacts/ or memory files

rate_limits:
  fetch_per_minute: 30
  shell_commands_per_minute: 60

stuck_after: 8                       # iterations without progress → terminate
---
```

### Mode A — `task_list` (bounded, ralph-like)

For objectives that decompose into discrete checkable items.

```yaml
mode: task_list
tasks:
  - id: enumerate_endpoints
    description: discover all reachable endpoints under challenge-7
    done: "file_exists:./artifacts/endpoints.txt AND line_count:./artifacts/endpoints.txt:>=1"
    passes: false
  - id: identify_vuln_class
    description: identify the vulnerability class exploitable in the target
    done: "file_exists:./findings/vuln_class.md"
    passes: false
  - id: achieve_rce
    description: obtain code execution on target
    done: "line_match:./artifacts/proof.txt:flag\\{.*\\}"
    passes: false
```

Terminates successfully when all tasks have `passes: true`. Kerkov updates `passes` by writing to `harness.md` or a sibling `tasks.json`.

### Mode B — `goal_predicate` (open-ended, bounded by stuck-detection)

For objectives that can't be decomposed upfront (recon, fuzzing, vulnerability research).

```yaml
mode: goal_predicate
goal: |
  Identify and document any authentication bypass in example.com.
  Produce a reproducible PoC script, a written bug report, and at least
  two independent verifications that the bug works.

success_criteria:
  - description: "reproducible PoC script exists"
    check: "file_exists:./artifacts/poc.py"
  - description: "PoC was run successfully at least twice"
    check: "line_count:./artifacts/verifications.log:>=2"
  - description: "bug writeup exists with endpoint + steps"
    check: "file_exists:./findings/bug-01.md AND line_count:./findings/bug-01.md:>=10"

progress_signal: "new line appended to findings.md OR new file under ./artifacts/"
```

Terminates successfully when all `success_criteria` check. Terminates unsuccessfully when `stuck_after` iterations pass with no `progress_signal` firing.

### Predicate DSL

Small, purpose-built. No shell execution from predicates (security risk).

| Check | Meaning |
|---|---|
| `file_exists:<path>` | path relative to run dir exists |
| `line_count:<path>:>=N` / `:==N` / `:<=N` | line count comparison |
| `line_match:<path>:<regex>` | any line matches regex |
| `content_match:<path>:<regex>` | whole file matches regex |
| `<pred> AND <pred>` / `<pred> OR <pred>` | boolean composition |

Predicates are evaluated by the driver, not by Kerkov. The model can't lie about completion.

## State contract (`state.json`)

Written at the end of each iteration by a small built-in **`HarnessStateTool`** (not by free-form model output — the model should call a tool that validates and writes the file, so the driver always gets valid JSON). Read by driver.

```json
{
  "iteration": 12,
  "status": "continue | complete | stuck | error",
  "last_progress_iteration": 10,
  "tokens_used_total": 1_823_441,
  "tokens_used_this_iteration": 148_221,
  "wallclock_seconds_total": 8_412,
  "cost_usd_total": 9.47,
  "notes": "short human-readable status from Kerkov",
  "success_criteria_results": [
    {"id": "poc_exists", "met": true},
    {"id": "verified_twice", "met": false}
  ]
}
```

**Important:** `status` is what Kerkov *thinks*. The driver authoritatively checks predicates independently and uses the intersection: a run is only `complete` if **both** Kerkov says so and the predicate evaluator agrees. This prevents early-exit hallucinations.

**Who triggers the write:** `run_harness_iteration()` detects `HarnessStateTool` being called as the model's final tool and exits cleanly once it is. That is the primary termination path and the common case. Two failure modes remain:

1. **Model produces a final text response without calling `HarnessStateTool`** ("abandoned"). Exit code signals this to the driver.
2. **Tool-iteration budget is hit before the model calls `HarnessStateTool`** ("budget exhausted"). Exit code signals this too.

For both of these, the driver falls back to **filesystem reconstruction**: diff artifact and memory files against the previous iteration's git snapshot (see "Git snapshotting", resolved from open question 4) and treat the iteration as `continue` with progress inferred from the diff. Only treat an iteration as `error` if the subprocess itself exited with a hard-error code. This guarantees one forgetful or runaway iteration cannot terminate a run that actually made progress.

## Memory files — who writes, what, when

| File | Writer | Format | Purpose |
|---|---|---|---|
| `harness.md` | user (or template) | YAML frontmatter + prose | Goal, scope, rules. Immutable during run. |
| `progress.txt` | Kerkov | append-only, one learning per line | "what I discovered this iteration" |
| `findings.md` | Kerkov | structured markdown | Ranked, de-duplicated findings |
| `dead_ends.md` | Kerkov | append-only | Approaches attempted and abandoned — prevents re-try across restarts |
| `AGENTS.md` | Kerkov | markdown | Patterns / gotchas specific to this run (analogous to ralph's AGENTS.md) |
| `inbox/*.md` | user | free-form | Optional. Driver prepends each file in filename order at the start of the next iteration, then moves them to `inbox/consumed/`. Queueing (vs. a single `interject.md`) avoids races when the user writes two notes before the next iteration starts. |
| `state.json` | `HarnessStateTool` | JSON | See above. Model calls the tool; tool validates and writes. |
| `tasks.json` (task_list mode only) | `HarnessStateTool` | JSON | Mirrors task_list with current `passes` values |

The harness.md system prompt tells Kerkov to read all memory files at start of every iteration, and instructs when to append to each.

## Driver loop (pseudocode)

```python
def run_loop(run_dir: Path) -> RunResult:
    harness = load_harness(run_dir / "harness.md")
    state = load_or_init_state(run_dir / "state.json")

    while True:
        state.iteration += 1

        # 1. Hard budget enforcement
        if state.exceeds(harness.budget):
            return RunResult.BUDGET_EXCEEDED

        # 2. Stuck detection
        if state.iteration - state.last_progress_iteration > harness.stuck_after:
            return RunResult.STUCK

        # 3. Predicate check (authoritative completion)
        if evaluate_success_criteria(harness, run_dir):
            return RunResult.COMPLETE

        # 4. User interjection (FIFO queue)
        drain_inbox(run_dir)

        # 5. Fresh Kerkov subprocess
        iter_dir = run_dir / "iterations" / f"{state.iteration:04d}"
        exit_code = spawn_kerkov(run_dir, iter_dir, non_interactive=True)

        # 6. Read what Kerkov reported
        new_state = read_state(run_dir / "state.json")
        if progress_detected(run_dir, state, new_state):
            new_state.last_progress_iteration = state.iteration
        state = new_state

        # 7. Kerkov's self-reported terminal status (must agree with predicate)
        if state.status in ("complete", "error"):
            # complete only wins if predicate already agreed above; otherwise continue
            if state.status == "error":
                return RunResult.ERROR

        # 8. Optional: git snapshot per iteration
        if harness.snapshot_per_iteration:
            git_commit(run_dir, message=f"iteration {state.iteration}")
```

## Interjection

User can inject guidance between iterations by dropping any `*.md` file into `./harnesses/runs/<slug>/inbox/`. At the start of the next iteration, the driver:

1. Lists `inbox/*.md` sorted by filename.
2. Concatenates their contents with a clear marker (`## USER INTERJECTION`) and prepends to the iteration prompt.
3. Moves them into `inbox/consumed/` (retain for audit; don't delete).

This is lower-friction than tearing down a running iteration. A directory queue (rather than a single `interject.md`) avoids the race where the user writes a second note before the first is consumed. For hard stops, the user Ctrl-Cs the driver — the current iteration finishes or is killed, depending on signal handling.

## Scope enforcement

Three layers, cheap to expensive:

1. **Prompt-level (v1):** `harness.md` contains explicit `in_scope` / `out_of_scope`. System prompt instructs the model to refuse out-of-scope actions. Trust-the-model baseline.
2. **Tool-level (v2):** `FetchTool` and `ShellTool` accept a scope policy at construction. Fetch rejects URLs outside `in_scope`. Shell rejects commands matching forbidden patterns. Requires wiring scope into tool init.
3. **Process-level (v3):** Sandbox the subprocess (network namespace, seccomp). Out of scope for this plan.

v1 is the MVP **only for trusted local/CTF targets**. For any run that touches an external system, v2 (tool-layer enforcement) is a **hard prerequisite**, not a fast follow — with Offensive mode + `ALLOW_AUTONOMOUS=true`, prompt-only scope is a liability.

**Launch gate (enforced in driver, Phase 1):** every harness must declare `target_class: local | ctf | external`. The driver refuses to launch a run with `target_class: external` until Phase 3 tool-layer scope enforcement has landed. This is not a recommendation — it's a hard abort at `kerkov loop` startup. Templates that target external systems (`external-recon.md`, `webapp-pentest.md`) ship with `target_class: external` so they are unrunnable until the safety rails exist.

## Rate limiting

Per-tool limits declared in harness (`rate_limits.fetch_per_minute`, etc.) are enforced by the tool layer using a token-bucket sidecar file in the run directory. Prevents:

- IDS triggers from uncapped request bursts
- Cloud provider TOS violations
- Kerkov accidentally DoSing itself

## Reference templates (ship with v1)

Each template fills in harness.md with realistic defaults the user edits:

- **`ctf.md`** — `task_list` mode. Scope locked to CTF target. Flag regex as terminal predicate.
- **`external-recon.md`** — `goal_predicate` mode. Goal: "produce asset inventory and exposure report." Deliverables: `artifacts/inventory.json`, `findings/exposure.md`.
- **`webapp-pentest.md`** — `goal_predicate` mode. Scope: one webapp. Deliverables: OWASP-top-10 findings report.
- **`binary-exploit-dev.md`** — `task_list` mode. Tasks: reverse → identify bug class → write primitive → achieve code execution.

Templates double as documentation for how to author harnesses.

## Termination conditions (summary)

A run ends when **any** of:

1. **Success** — all predicates in `success_criteria` (goal_predicate) or all `tasks[].passes` (task_list) evaluate true
2. **Budget exceeded** — tokens, wallclock, iterations, or cost cap breached
3. **Stuck** — `stuck_after` iterations without progress signal firing
4. **Error** — Kerkov subprocess exits non-zero or writes `status: error`
5. **User interrupt** — Ctrl-C on driver

Every termination writes a final summary to `run_dir/summary.md`: outcome, iteration count, artifacts produced, cost, reason.

## Phased implementation

### Phase 1 — Minimum viable driver (local/CTF targets only)

The smallest cut that runs end-to-end against a trivial local CTF. Budget enforcement is intentionally coarse; the next phases add the safety rails needed for anything external.

- New subcommand: `kerkov loop <run_dir>`
- Harness parser (frontmatter + markdown body) for **`task_list` mode only**
- Predicate DSL evaluator (`file_exists`, `line_count`, `line_match`, AND/OR)
- New CLI flag: `--harness-iteration <run_dir>` on `src/cli/main.py`. Dispatches to a new `KerkovCLI.run_harness_iteration()` method instead of `run_non_interactive()`. The method:
  - Loads `harness.md`, `progress.txt`, `findings.md`, `dead_ends.md`, `AGENTS.md` from the run dir and appends their contents to the base system prompt built in `kerkov_cli.py:504-513` (after the mode prompt, before the autonomy marker). Not via `_transient_system_prompts` — see rationale in "Tool-loop engine for a single iteration".
  - Forces autonomy level AUTONOMOUS (requires `ALLOW_AUTONOMOUS=true`).
  - Seeds the conversation with a single opening user message derived from the harness and current progress.
  - Runs the same inner tool loop `UserMessage.execute()` already drives.
  - Terminates on: (a) `HarnessStateTool` called as the final tool — exit `0`, (b) model produces a final text response without calling it — exit `10` (abandoned), (c) tool-iteration budget hit — exit `11` (budget exhausted), (d) unhandled exception — exit `>20` (hard error).
  - Writes `iterations/NNNN/exit.json` with reason + token totals before exiting.
- `run_non_interactive()` is left untouched; the two entry points share the tool loop but have different termination contracts.
- Expose `ctf` in `main.py` `--mode` choices (currently restricted to `recon/audit/offensive` at `src/cli/main.py:42`). `ModeType.CTF` exists in the enum and has a full mode definition in `src/cli/modes.py:127-140`, it's just not CLI-selectable. `ctf.md` template needs this.
- New built-in `HarnessStateTool` (writes validated `state.json` at end of iteration)
- Subprocess spawn with `ALLOW_AUTONOMOUS=true` + autonomy level 4 set in child env (reuses existing mechanism, no new approval mode)
- Coarse budgets only: `max_iterations`, `max_wallclock_seconds`
- Progress detection: new file under `artifacts/` **or** a new findings line that references a new artifact path (line-count alone is too easy to game)
- One reference template: `ctf.md` (`target_class: ctf`)
- **Launch gate:** driver aborts if `target_class: external`; only `local` and `ctf` are runnable in Phase 1
- **Budget parser:** Phase 1 accepts `max_iterations` and `max_wallclock_seconds`. If `max_tokens` or `max_cost_usd` appear in a harness, parser emits a warning that they are not yet enforced (Phase 2) rather than silently ignoring them
- **Git snapshotting is Phase 1, not Phase 4:** on `kerkov loop` startup, if `run_dir` is not already a git repo, driver runs `git init` inside it (no relation to the parent repo — see open question 4, resolved). Driver commits after every iteration before reading `state.json`. The fallback in the state-contract section depends on this snapshot existing, so git cannot be deferred to polish
- Test: run a trivial local CTF harness end-to-end

### Phase 2 — `goal_predicate` mode + stuck detection + token/cost budgets + prompt caching

- Add `goal_predicate` mode to parser
- Stuck detection in driver (`stuck_after` iterations without progress signal)
- Per-provider USD pricing tables + `CostTracker`; promote `max_tokens` and `max_cost_usd` from "parsed with warning" to enforced
- Anthropic `cache_control: ephemeral` wiring in `src/providers/anthrophic_provider.py` for the stable harness-context prefix (see open question 8) — bundled here because long `goal_predicate` runs are where cold-cache resends actually cost money
- Two more templates, shipped with `target_class: external` (still unrunnable until Phase 3): `external-recon.md`, `webapp-pentest.md`

### Phase 3 — Scope and rate enforcement at tool layer **(hard prerequisite for any external-target run)**

- `FetchTool` accepts scope policy, rejects out-of-scope hosts
- Shell tools accept a forbidden-pattern list; wire `REQUIRE_SUDO_APPROVAL` into execution (currently declared in config but unused)
- Token-bucket rate limiter shared across subprocess restarts via a file in the run dir
- Flip the Phase 1 launch gate: driver now accepts `target_class: external` once this phase is green. Until then, harnesses shipped in Phase 2 with `target_class: external` remain unrunnable by design.

### Phase 4 — Interjection + polish

- `inbox/*.md` consumption with `inbox/consumed/` archival
- `summary.md` generation on termination
- `kerkov loop status <run_dir>` — read-only status command

### Phase 5 — Harness generator (deferred)

- Separate LLM-driven utility (not Kerkov) that takes a user description and outputs a harness.md draft. Outside KerkovCLI so it doesn't inherit the security system prompt.

## Open questions / prerequisites

1. ~~**Kerkov non-interactive contract.**~~ **Resolved.** `src/cli/main.py:19-74` exposes `--message` (repeatable), `--no-welcome`, `--mode`. `src/cli/kerkov_cli.py:231-245` implements `run_non_interactive()` which processes messages and exits cleanly. `scripts/ci/run_vulhub_e2e.py:130-141` already drives the CLI in exactly this pattern. Remaining work for Phase 1 is additive: `--harness-context <dir>` hooking into base system prompt assembly at `src/cli/kerkov_cli.py:504-513`, plus exposing `ctf` in `main.py` `--mode` choices.

2. ~~**Tool approval in a loop.**~~ **Resolved.** Don't invent a new approval mode. `AutonomyManager` level 4 (AUTONOMOUS) + `ALLOW_AUTONOMOUS=true` env var already auto-approves every tool (`src/cli/autonomy_manager.py:17-31,63,103`; `src/cli/tool_approval_helper.py:36-80`). The driver sets both in the subprocess env. Scoping is enforced at the tool layer in Phase 3, not via approval gating.

3. **Cost tracking.** Still open. `TokenUsage` in `src/models/basic_models.py:4-8` tracks tokens per call; `src/context/token_tracker.py` accumulates context size but not USD. Need per-provider pricing tables and cross-iteration aggregation before `max_cost_usd` can be enforced. Phase 1 avoids this by enforcing only `max_iterations` and `max_wallclock_seconds`; Phase 2 adds it.

4. ~~**Git snapshotting semantics.**~~ **Resolved.** `run_dir` is its own git repo, initialized by the driver on first launch (`git init` inside the run dir if no `.git` exists). This is independent of any parent repo — iteration commits go into the run-dir's own history and never pollute the surrounding project. Promoted to Phase 1 because the `state.json` reconstruction fallback (question 7) requires per-iteration snapshots to diff against. `./harnesses/runs/` should be added to the main project's `.gitignore`.

5. **Transcript storage — reinvent or reuse?** `src/cli/engagement_store.py` already persists full message JSONL and supports compaction via `override_engagement_messages()`. Decision: whether `iterations/NNNN/transcript.jsonl` is a fresh file written by the driver, or whether each iteration creates an engagement and we link `engagement_id` from `state.json`. Reuse is preferred unless engagement semantics get in the way.

6. **Concurrent runs.** v1 is single-run. Multi-run (different harnesses in parallel) is deferred — adds token-bucket coordination complexity.

7. ~~**Guaranteed `state.json` write at iteration end.**~~ **Resolved.** The original framing assumed `run_non_interactive()` was the entry point, which drains `--message` inputs and exits — giving no hook for "run until `HarnessStateTool` is called." The new `run_harness_iteration()` method flips that: the tool loop's termination condition *is* `HarnessStateTool` being called as the final tool, so the happy path is guaranteed by construction. The two remaining failure modes — model abandons without calling the tool, or tool-iteration budget is exhausted — are signalled by distinct exit codes, and the driver falls back to filesystem/git-diff reconstruction (see "State contract"). Mandatory-final-message (old option a) and post-loop hook (old option b) are both obsolete.

8. **Prompt caching across iterations.** Each fresh subprocess is a cold cache on the provider side. Harness context (harness.md + findings.md + dead_ends.md + AGENTS.md) grows into many KB over a long run and gets retokenized every iteration, so the cost of cold-cache resends scales with run length — a 50-iteration run re-pays the full prefix 50 times. For Anthropic, stable-prefix caching with `cache_control: ephemeral` on the harness context block is a straightforward win. Needs wiring in `src/providers/anthrophic_provider.py` (no cache_control wiring today). **Phase 2 work**, bundled with `CostTracker` — the same piece of code that reports USD spend will want the cache-hit numbers to be honest, and long-running `goal_predicate` runs (which land in Phase 2) are where cold-cache waste actually bites. Not a Phase 1 blocker because Phase 1 runs are bounded by `max_iterations` only.

## Risks (and how the design addresses them)

| Risk | Mitigation |
|---|---|
| Unbounded cost | `max_tokens`, `max_cost_usd`, `max_wallclock_seconds` as hard caps in driver |
| Scope creep (model pivots to adjacent host) | Explicit `in_scope` / `out_of_scope` + tool-layer enforcement in Phase 3 |
| Loop that mistakes motion for progress | `progress_signal` must reference a new artifact path, not just a new line; `stuck_after` stops runs going nowhere |
| Model hand-writes invalid `state.json` | `HarnessStateTool` validates and writes; model cannot freehand the file |
| Detection/noise at target | `rate_limits` enforced at tool layer (Phase 3) |
| Early-exit hallucination ("I'm done!") | Driver re-evaluates predicates independently of Kerkov's self-report |
| Re-trying the same dead ends across restarts | `dead_ends.md` read at start of every iteration |
| User can't interrupt mid-loop cleanly | `inbox/*.md` consumed between iterations; Ctrl-C on driver between iterations |
| Autonomous external run with no tool-layer scope | Phase 3 is a hard prerequisite for external targets, not a fast follow |
| Harness author writes lazy harness → garbage run | Required-field schema validation at launch; ship reference templates |

## Open naming decisions

- **Subcommand:** `kerkov loop` vs `kerkov run` vs `kerkov harness`. Preference: `kerkov loop <run_dir>` (action-oriented, matches mental model of "loop on this directory").
- **Run slug convention:** `YYYY-MM-DD-<name>` or UUID. Preference: dated slug (human-scannable).
- **Interjection mechanism:** `inbox/*.md` queue (chosen) vs single `interject.md`. The directory queue avoids the race where a second user note overwrites the first before consumption.
