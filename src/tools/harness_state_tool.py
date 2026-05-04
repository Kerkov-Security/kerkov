"""HarnessStateTool — terminal tool for harness iterations.

Each harness iteration ends when the model calls this tool. The tool validates
the model's report against the HarnessState schema and writes state.json with
authoritative cumulative token + cost totals (the model's self-report cannot
forge token counts — they come from the in-process iteration accumulator).

Constructed per-iteration with a bound run_dir + harness, so it is NOT included
in the default ToolSuite — it is appended to self.tools by run_harness_iteration().
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, List, Optional, Tuple

from pydantic import Field, ValidationError

from src.harness.models import Harness, HarnessState, HarnessStatus, TaskResult
from src.harness.pricing import compute_cost_usd
from src.harness.state import load_state, save_state
from src.tools.base_tool import BaseTool, ToolApprovalLevel, ToolParams, ToolResult


class HarnessStateToolParams(ToolParams):
    status: str = Field(
        description="One of: continue | complete | stuck | error. "
        "Use 'continue' if more work remains; 'complete' only if you believe all success "
        "criteria are met (the driver re-checks); 'stuck' if you cannot make progress; "
        "'error' on hard failure."
    )
    notes: str = Field(
        default="",
        description="Short human-readable status, 1-3 sentences. What you did this iteration "
        "and what should happen next.",
    )
    success_criteria_results: Optional[List[dict]] = Field(
        default=None,
        description="Optional list of {id, met} dicts mirroring the harness success_criteria. "
        "IDs must match those declared in harness.md. Driver evaluates predicates "
        "independently — this is for your self-report only.",
    )
    tasks_passed: Optional[List[str]] = Field(
        default=None,
        description="task_list mode only. List of task IDs you believe you completed this "
        "iteration. IDs must match tasks declared in harness.md.",
    )
    forfeit_reason: Optional[str] = Field(
        default=None,
        description="Required if status is 'stuck' or 'error'. Short explanation of why "
        "you cannot continue.",
    )


class HarnessStateTool(BaseTool):
    """Terminal tool: writes state.json and signals iteration completion."""

    NAME = "harness_state"

    def __init__(
        self,
        run_dir: Path,
        current_iteration: int,
        harness: Harness,
        usage_provider: Callable[[], Tuple[int, int]],
        model_name: str,
    ):
        """
        Args:
            run_dir: Run directory containing harness.md and state.json
            current_iteration: 1-indexed iteration number this tool serves
            harness: Parsed harness for ID cross-validation
            usage_provider: Callable returning (prompt_tokens, completion_tokens)
                for this iteration only. Read at run() time so it captures the
                final post-call totals, not a stale snapshot.
            model_name: Active provider model name, used for pricing lookup
        """
        super().__init__(
            name=self.NAME,
            description=(
                "Report your status at the end of this harness iteration. "
                "Call this tool exactly once, as the FINAL action of the iteration, "
                "before returning your final message. The driver uses your status "
                "(combined with its own predicate checks) to decide whether to spawn "
                "another iteration."
            ),
        )
        self._run_dir = run_dir
        self._iteration = current_iteration
        self._harness = harness
        self._task_ids = {t.id for t in harness.tasks}
        self._criterion_ids = {c.id for c in harness.success_criteria}
        self._usage_provider = usage_provider
        self._model_name = model_name

    def get_approval_level(self) -> ToolApprovalLevel:
        return ToolApprovalLevel.NONE

    def get_params_class(self):
        return HarnessStateToolParams

    def validate_params(self, toolParams: HarnessStateToolParams) -> Optional[str]:
        try:
            status = HarnessStatus(toolParams.status)
        except ValueError:
            return (
                f"status must be one of: "
                f"{', '.join(s.value for s in HarnessStatus)} "
                f"(got {toolParams.status!r})"
            )

        if status in (HarnessStatus.STUCK, HarnessStatus.ERROR):
            if not (toolParams.forfeit_reason and toolParams.forfeit_reason.strip()):
                return (
                    f"forfeit_reason is required when status is {status.value!r}"
                )

        if toolParams.tasks_passed:
            unknown = [tid for tid in toolParams.tasks_passed if tid not in self._task_ids]
            if unknown:
                return (
                    f"tasks_passed contains ids not declared in harness.md: {unknown}. "
                    f"Known task ids: {sorted(self._task_ids) or '(none)'}"
                )

        if toolParams.success_criteria_results:
            for entry in toolParams.success_criteria_results:
                if not isinstance(entry, dict) or "id" not in entry:
                    return (
                        "success_criteria_results entries must be {'id': str, 'met': bool} "
                        f"(got {entry!r})"
                    )
                cid = entry["id"]
                if self._criterion_ids and cid not in self._criterion_ids:
                    return (
                        f"success_criteria_results id {cid!r} not declared in harness.md. "
                        f"Known criterion ids: {sorted(self._criterion_ids) or '(none)'}"
                    )

        return None

    async def run(self, toolParams: HarnessStateToolParams) -> ToolResult:
        try:
            results: List[TaskResult] = []
            if toolParams.success_criteria_results:
                for entry in toolParams.success_criteria_results:
                    try:
                        results.append(TaskResult(**entry))
                    except ValidationError as exc:
                        return ToolResult(
                            success=False,
                            output="",
                            error=f"success_criteria_results entry invalid: {exc}",
                        )

            notes = toolParams.notes
            if toolParams.forfeit_reason:
                notes = (
                    f"{notes}\n\nforfeit_reason: {toolParams.forfeit_reason}"
                    if notes
                    else f"forfeit_reason: {toolParams.forfeit_reason}"
                )

            prompt_tokens, completion_tokens = self._usage_provider()
            iter_tokens = prompt_tokens + completion_tokens
            iter_cost = compute_cost_usd(self._model_name, prompt_tokens, completion_tokens)
            cost_known = iter_cost is not None

            prior = load_state(self._run_dir)
            cumulative_tokens = prior.tokens_used_total + iter_tokens
            cumulative_cost = prior.cost_usd_total + (iter_cost or 0.0)
            # Once cost tracking is broken (unknown model on any iteration), the
            # cumulative number is no longer trustworthy for budget enforcement.
            cost_tracking_enabled = prior.cost_tracking_enabled and cost_known

            state = HarnessState(
                iteration=self._iteration,
                status=HarnessStatus(toolParams.status),
                last_progress_iteration=prior.last_progress_iteration,
                tokens_used_total=cumulative_tokens,
                tokens_used_this_iteration=iter_tokens,
                cost_usd_total=cumulative_cost,
                cost_usd_this_iteration=iter_cost or 0.0,
                cost_tracking_enabled=cost_tracking_enabled,
                notes=notes,
                success_criteria_results=results,
            )
            save_state(self._run_dir, state)

            cost_str = (
                f"${iter_cost:.4f} this iter / ${cumulative_cost:.4f} total"
                if cost_known
                else "cost-tracking unavailable (unknown model pricing)"
            )
            return ToolResult(
                success=True,
                output=(
                    f"Recorded iteration {self._iteration} state: "
                    f"status={state.status.value}, "
                    f"tokens={iter_tokens} this iter / {cumulative_tokens} total, "
                    f"{cost_str}. Driver will read state.json now."
                ),
                metadata={"terminal": True, "status": state.status.value},
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                output="",
                error=f"Failed to write state.json: {exc}",
            )
