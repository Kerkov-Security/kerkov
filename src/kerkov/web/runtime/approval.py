"""Web approval strategy for Kerkov tool calls."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any, Optional

from kerkov.cli.approval_mode_manager import ApprovalMode, ApprovalModeManager
from kerkov.cli.approval_rules import (
    AllowRuleChoice,
    ApprovalRule,
    ApprovalRulesEngine,
    allow_rule_choices,
    append_rule_to_file,
    render_call_string,
)
from kerkov.cli.tool_approval_helper import ToolApprovalResult
from kerkov.core.conversation_engine import ApprovalStrategy
from kerkov.core.logging.logger_factory import get_logger
from kerkov.tools.base_tool import BaseTool
from kerkov.web.runtime.events import WebEventBus

logger = get_logger("WebApproval")


class WebApprovalController:
    """Tracks browser approval prompts that the engine is waiting on."""

    def __init__(self, bus: WebEventBus) -> None:
        self._bus = bus
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def request(
        self,
        tool: BaseTool,
        params: dict[str, Any],
        *,
        choices: dict[str, AllowRuleChoice],
    ) -> dict[str, Any]:
        approval_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[approval_id] = future

        self._bus.emit(
            "approval.request",
            {
                "approval_id": approval_id,
                "tool": tool.name,
                "params": params,
                "call": render_call_string(tool.name, params),
                "allow_rule_choices": [
                    {
                        "decision": decision,
                        "label": choice.label,
                        "rule": choice.rule_string,
                    }
                    for decision, choice in choices.items()
                ],
            },
        )

        try:
            return await future
        finally:
            self._pending.pop(approval_id, None)
            if future.cancelled():
                self._bus.emit(
                    "approval.cancelled",
                    {"approval_id": approval_id, "detail": "Tool approval cancelled."},
                )

    def resolve(
        self,
        approval_id: str,
        decision: str,
        user_message: Optional[str] = None,
    ) -> bool:
        future = self._pending.get(approval_id)
        if future is None or future.done():
            return False
        future.set_result({"decision": decision, "user_message": user_message})
        return True

    def cancel_all(
        self, *, reason: str = "Browser session disconnected before approval."
    ) -> None:
        for approval_id, future in self._pending.items():
            if not future.done():
                future.set_result(
                    {
                        "decision": "deny",
                        "user_message": reason,
                    }
                )
                self._bus.emit(
                    "approval.cancelled",
                    {"approval_id": approval_id, "detail": reason},
                )
        self._pending.clear()


class WebApprovalStrategy(ApprovalStrategy):
    """Apply approval rules and prompt through the browser when needed."""

    def __init__(
        self,
        *,
        bus: WebEventBus,
        controller: WebApprovalController,
        rules_engine: ApprovalRulesEngine,
        mode_manager: ApprovalModeManager,
    ) -> None:
        self._bus = bus
        self._controller = controller
        self._rules_engine = rules_engine
        self._mode_manager = mode_manager
        self._workspace_approvals_path: Optional[Path] = None

    def set_workspace(self, workspace_path: Path) -> None:
        approvals_file = workspace_path / "approvals.yaml"
        self._workspace_approvals_path = approvals_file

        if approvals_file.exists():
            engine = ApprovalRulesEngine.from_file(approvals_file)
            self._rules_engine.set_workspace_rules(engine.file_rules)

    def clear_session_rules(self) -> None:
        self._rules_engine.clear_session_rules()
        self._rules_engine.set_workspace_rules([])
        self._workspace_approvals_path = None

    async def approve(self, tool: BaseTool, params: dict) -> ToolApprovalResult:
        approval_mode = self._mode_manager.current

        if approval_mode == ApprovalMode.YOLO:
            self._emit_auto(tool, params, approved=True, reason="yolo mode")
            return ToolApprovalResult(approved=True)

        rule_result = self._rules_engine.evaluate(tool.name, params)
        if rule_result == "deny":
            self._emit_auto(tool, params, approved=False, reason="deny rule")
            return ToolApprovalResult(approved=False)

        if approval_mode != ApprovalMode.STRICT and rule_result == "allow":
            self._emit_auto(tool, params, approved=True, reason="allow rule")
            return ToolApprovalResult(approved=True)

        if approval_mode == ApprovalMode.PERMISSIVE:
            self._emit_auto(tool, params, approved=True, reason="permissive mode")
            return ToolApprovalResult(approved=True)

        offered = allow_rule_choices(tool, params)
        choices = {}
        if len(offered) > 1:
            choices["approve_save_broad"] = offered[0]
        if offered:
            # Older clients sending approve_save get only the exact scope.
            choices["approve_save"] = offered[-1]
        response = await self._controller.request(tool, params, choices=choices)

        decision = response.get("decision")
        user_message = response.get("user_message") or None

        if decision in {"approve", "approve_once"}:
            return ToolApprovalResult(approved=True)

        if decision in choices:
            self._save_rule(choices[decision].rule)
            return ToolApprovalResult(approved=True)

        return ToolApprovalResult(approved=False, user_message=user_message)

    def _emit_auto(
        self,
        tool: BaseTool,
        params: dict[str, Any],
        *,
        approved: bool,
        reason: str,
    ) -> None:
        self._bus.emit(
            "approval.auto",
            {
                "tool": tool.name,
                "params": params,
                "approved": approved,
                "reason": reason,
            },
        )

    def _save_rule(self, rule: ApprovalRule) -> None:
        """Persist the rule shown for the operator's selected scope."""
        if self._workspace_approvals_path is not None:
            self._rules_engine.add_workspace_rule(rule)
            try:
                append_rule_to_file(self._workspace_approvals_path, rule)
            except Exception as exc:
                logger.warning(
                    "Could not persist approval rule to workspace: %s",
                    exc,
                )
                self._bus.emit(
                    "log.warning",
                    {
                        "message": (
                            "Approval granted, but the allow rule could not be "
                            "saved to the workspace. Check the server logs."
                        )
                    },
                )
        else:
            self._rules_engine.add_session_rule(rule)
