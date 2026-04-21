# Harness Feature — Brief Explanation

**Related:** [harness-plan.md](harness-plan.md) (full design)

## What it is, in one paragraph

Let the user write a **harness** — a markdown file with frontmatter declaring a
cybersecurity goal, scope, success criteria, and budgets — then run
`kerkov loop <run_dir>` to execute it autonomously. A dumb **driver** process
(no LLM calls) loops. Each iteration spawns a fresh Kerkov subprocess in
fully-autonomous mode. The subprocess reads the harness and persistent memory
files (`progress.txt`, `findings.md`, `dead_ends.md`, `AGENTS.md`), does work,
writes state via a built-in `HarnessStateTool`, and exits. The driver evaluates
success predicates independently of what the model claims, commits the run dir
to git, and loops. Fresh subprocesses solve context pollution without needing
in-process compaction (ralph pattern).

## Two harness modes

- **`task_list`** — discrete checkable items; terminal when all pass.
- **`goal_predicate`** — open-ended; terminal when success predicates fire, or
  stuck-detection trips after N iterations with no progress signal.

## Termination conditions

Success, budget breach, stuck, error, or Ctrl-C. Every termination writes a
`summary.md` with outcome, cost, and reason.

## Phasing

- **Phase 1** — driver + `task_list` + `ctf.md` template + git snapshots +
  `state.json` + launch gate refusing `target_class: external`.
- **Phase 2** — `goal_predicate` mode + cost tracking + Anthropic prompt
  caching + more templates (shipped but unrunnable via the launch gate).
- **Phase 3** — tool-layer scope and rate enforcement. This phase flips the
  launch gate open, which is the only thing that makes external-target runs
  possible.
- **Phase 4** — inbox interjection, `summary.md` generation, read-only status
  command.
- **Phase 5** — harness generator utility (deferred, and the only piece that
  belongs outside this repo — see below).

## Key safety choices

- Predicates are evaluated by the **driver**, not the model. A model that
  claims "I'm done!" does not end the run unless the filesystem agrees.
- `HarnessStateTool` writes validated JSON — the model cannot freehand
  `state.json`.
- `target_class: external` is a **hard driver abort** until Phase 3 ships.
  Templates aimed at external systems are shipped with this flag so they are
  unrunnable by design until the tool-layer scope enforcement exists.
- Per-iteration git commits give an audit trail *and* the fallback that lets
  the driver reconstruct state when the model forgets to call
  `HarnessStateTool` — so one forgetful iteration cannot terminate a run that
  actually made progress.

## What `HarnessStateTool` does, exactly

`HarnessStateTool` is a normal built-in tool (same pattern as `ShellTool`,
`FetchTool`) that the model calls as its final action in every iteration. It
sits in `src/tools/`, extends `BaseTool`, and has approval level `NONE` — it
is trusted because its blast radius is confined to the run directory and its
schema is validated at the tool boundary.

**Input (Pydantic `HarnessStateParams`):**

| Field | Meaning |
|---|---|
| `status` | Literal enum: `continue` / `complete` / `stuck` / `error`. This is what the model *thinks*. |
| `notes` | Short human-readable summary of this iteration — what was tried, what was learned. Captured in transcript for audit. |
| `success_criteria_results` | List of `{id, met, evidence_path?}`. IDs must match `success_criteria` declared in `harness.md`. |
| `tasks_passed` | List of task IDs the model believes it completed this iteration (`task_list` mode only). IDs must match `tasks[].id`. |
| `findings_appended` | Bool. Whether the model appended to `findings.md` — a progress hint for the driver. |
| `forfeit_reason` | Optional string — required if `status` is `stuck` or `error`. |

**Behavior, step by step:**

1. **Read harness.md** and validate: every `success_criteria_results[].id` and
   every `tasks_passed[]` id must exist in the harness. Reject the call with
   a `ToolResult(success=False)` if not — the model gets a clear error and
   another tool turn to retry.
2. **Read the current `state.json`** if present. The iteration counter is
   owned by the driver, not the tool, so the tool merges rather than increments.
3. **Pull token usage** from the in-process `TokenTracker`, not from the
   model's input. The model cannot forge token counts. Aggregate into
   `tokens_used_this_iteration`.
4. **Compute cost (Phase 2 only)** — look up per-provider pricing, multiply
   against input/output token counts, add to `cost_usd_total`. In Phase 1 this
   field stays at `0.0`.
5. **Compose the full state dict** and validate against the `state.json`
   schema (Pydantic). Wrong enum values, missing required fields, or IDs that
   don't match the harness all fail validation here.
6. **Write atomically** — write to `state.json.tmp`, then rename. This
   prevents a half-written file if the subprocess is killed mid-write.
7. **For `task_list` mode**, mirror the current `passes` state into
   `tasks.json` so the driver's predicate evaluator has a single source of
   truth for task completion.
8. **Return `ToolResult(success=True, output="state written at iteration N")`**.
   The model sees confirmation and exits the conversation loop cleanly.

**Why this is a tool and not a post-loop hook:**

- The tool call shows up in the transcript, so the state write is auditable
  in context with the reasoning that produced it.
- Schema validation at the tool boundary catches hallucinated field names or
  IDs *during* the iteration, so the model can correct itself. A post-loop
  hook would catch errors only after the subprocess already exited.
- `notes` captures the model's narrative summary of the iteration — useful
  reasoning state that a filesystem diff (the fallback path) can't recover.

**Why this is a tool and not "the model writes state.json via WriteTool":**

- `WriteTool` doesn't know the state schema. Validation would have to happen
  after the fact in the driver, at which point the iteration is already
  wasted.
- Token and cost counts are computed inside the tool from the trusted
  `TokenTracker`, not supplied by the model. A `WriteTool` path would let
  the model write whatever counts it wanted.

## Should this be its own repo?

**No — keep it in `kerkov-bot`.**

The driver is a thin subcommand that spawns `python -m src.cli.main` with
specific env/flags. Almost every non-trivial piece is tightly coupled to
internals you already own:

- `HarnessStateTool` is a tool in `src/tools/` alongside Shell/Fetch, and
  shares the `BaseTool` / `ToolResult` contract.
- `--harness-context <dir>` hooks into base system prompt assembly at
  `src/cli/kerkov_cli.py:504-513`.
- Phase 3 scope and rate enforcement edits `FetchTool` and the shell tools
  in place.
- Phase 2 cost tracking extends `TokenUsage` in `src/models/basic_models.py`
  and `src/context/token_tracker.py`.
- Exposing CTF mode on the CLI is a one-line argparse change in
  `src/cli/main.py:42`.

Splitting the driver out would create cross-repo dependency churn: every
tool change becomes a coordination problem. The existing end-to-end test
infra (`scripts/ci/run_vulhub_e2e.py`) also lives here.

`./harnesses/runs/` should be gitignored at the main project level regardless
— each run directory is its own git repo, and run artifacts should not
pollute the project's history.

**The one exception:** the Phase 5 harness generator — a separate LLM
utility that takes a user description and outputs a `harness.md` draft. The
plan explicitly calls for it to live outside `KerkovCLI` so it does not
inherit the security system prompt, skills, or tool schemas. That one
belongs in its own small repo or a sibling module when it is built.
