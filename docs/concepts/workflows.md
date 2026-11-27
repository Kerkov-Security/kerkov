# Workflows

How a guided workflow runs the same way from the terminal and the web console.
The operator-facing pages are [slash commands](../reference/slash-commands.md#workflows)
for the CLI and [compliance assessment](../reference/tools/compliance.md) for
the tool surface and the browser panel.

## Pieces

| Module | Role |
|---|---|
| `core/workflows/registry.py` | `WORKFLOW_DEFINITIONS`: each workflow's name, recommended modes, actions and static objective overlay |
| `core/workflows/context.py` | `WorkflowContext`, the front end's live state as getters; `WorkflowObserver` for progress |
| `core/workflows/compliance.py` | `ComplianceRun`: readiness, plan gating, result folding and the persisted run record |
| `core/workflows/output.py` | `WorkflowOutput`, a forwarding `OutputSink` that also hands each tool result to the run |
| `core/compliance/workflows.py` | `WorkflowRequest`, `WorkflowStore` and the before/after control comparison |
| `web/runtime/compliance.py` | Builds the context from a browser session and publishes `compliance.*` events |
| `cli/workflows/` | Setup prompts, the CLI turn and terminal rendering of the same record |

Nothing under `core/workflows/` imports `kerkov.web` or `kerkov.cli`.

## The registry

Both front ends read one list, the way they share `COMMAND_SPECS`. `/workflow`
lists it, dispatches through it, and a test requires a terminal handler for every
entry, so neither front end can offer a workflow the other lacks.

Compliance recommends Blue and Purple. CTF recommends Red. In other modes,
the workflow shows a warning and proceeds in the current engagement. The
`/mode` command starts a new engagement.

## Run and engagement scope

A definition's `engagement_scoped` flag says how long its overlay applies.

| Scope | Workflow | Overlay applies to | State |
|---|---|---|---|
| Run | Compliance | The turns of one gated run | The run record under `automation/compliance-runs/` |
| Engagement | CTF | Every main turn until stopped | `workflow` in the engagement's JSONL metadata |

`/workflow ctf start` starts the engagement if the conversation is empty, then
writes `workflow: "ctf"` with `EngagementStore.update_workflow()`.
`SessionKernel.build_loop_session()` reads `EngagementStore.workflow` and
prepends that definition's overlay to the turn's transient prompts, so both front
ends apply it without their own state. The store tracks the selected engagement:
starting one clears the workflow, loading one restores it, and `detach()` drops
it. `/clear`, `/mode` and resume need no extra reset (the CLI's `clear_history()`
detaches the store for that reason). Resume validates the saved name and fails
with `EngagementRestoreError` if it names no engagement-scoped workflow. Agents
build their own `LoopSession`s and do not receive the overlay.

## Prompt layers

| Layer | Content | Delivered as |
|---|---|---|
| Mode posture | `BASE_PROMPT` and the role overlay | System prompt, at engagement start |
| Workflow objective | The definition's `overlay`, e.g. Compliance's "assess first, preview is never verification" or CTF's flag objective and lab scope | A transient prompt: for a run's turn (Compliance), or for every main turn while active (CTF) |
| Per-run plan | Scan and remediation arguments, selected Ansible profile (`ComplianceRun.plan_message()`) | A `context_only` user message |

The split is static versus per-run. The overlay names no host, engagement ID or
timestamp, so it stays byte-identical and, as a trailing system message after
Anthropic's last cache breakpoint, invalidates nothing. It is not added to the
system prompt because the objective should stop applying when the run ends, and
because both front ends compose the system prompt only for an empty history, so
a workflow started mid-conversation could not reach it.

The CTF overlay follows the same rule on every turn: the trailing tail is
re-sent uncached, and the cached prefix is unchanged with or without it
(`tests/providers/anthropic_caching_test.py`).

Transient prompts no longer imply a one-shot invocation. The CLI's `--message`
path sets `LoopSession.retry_deferred_execution` for its single "act, don't
plan" nudge; a workflow turn carries a transient prompt without being nudged.

## The run context

`ComplianceRun` re-checks the engagement on every gated execution call: the
workspace, engagement identity, ledger row for the selected asset, execution
setting, scanner image and whether the session closed. `WorkflowContext`
therefore holds callables, not values. Freezing them at construction would
silently retire the "engagement or execution settings changed" block, which is
what keeps the model inside the approved plan after a resume or a settings change.

The run is the turn's `ApprovalStrategy`. It refuses calls outside the plan, then
delegates the rest to the front end's own approval path (`context.approval()`),
so workflow gating narrows what may run without bypassing the operator's
approval mode. Execution denial, changed plan files or asset identity, and a
failed record write block the run.

## Front ends

The web console builds a context from the browser session, emits
`compliance.updated` from its observer, and wraps the panel's output in
`compliance.event`. Its behavior and events are unchanged by the move into core;
see [web architecture](web-architecture.md).

The CLI binds the context to its current engagement, ledger and interactive
approval, narrates stage changes through its output sink, and runs the plan
through `KerkovCLI.run_workflow_turn()`, which ends the turn on interrupt so
`finish()` can record a stopped run as interrupted. `apply` re-issues a
completed preview's stored request with its `preview_id`, so its signature
matches without re-entering settings. Workflow runs are refused under
`--message`, whose non-interactive auto-approval would bypass the operator.

Both read the same records under the root workspace's
`automation/compliance-runs/`, and both render `record + control_comparison(record)`,
so a run started in one front end can be reviewed or applied from the other.
