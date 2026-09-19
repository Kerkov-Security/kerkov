"""The C5 context-offload guarantee (#147), held across the detached rewrite.

Delegation exists to keep an agent's noise out of the master's context. What
crosses the boundary is the report and nothing else — not the agent's tool
output, not its intermediate reasoning. Detaching `delegate` moved where the
report is produced, so this is pinned as a test rather than an assumption.
"""

import asyncio
import tempfile
import unittest
from copy import copy
from pathlib import Path
from typing import List
from unittest.mock import AsyncMock, MagicMock

from kerkov.agents.agent_manager import AgentManager
from kerkov.agents.agent_registry import AgentRegistry
from kerkov.agents.agent_supervisor import AgentSupervisor
from kerkov.agents.agent_tool import SpawnAgentTool
from kerkov.agents.agent_wait_tool import AgentWaitTool
from kerkov.agents.report_tool import REPORT_TOOL_NAME
from kerkov.core.approval_scope import ApprovalScope
from kerkov.core.conversation_engine import (
    ConversationEngine,
    LoopSession,
    QuietSink,
    WhitelistAutoApproval,
)
from kerkov.models.basic_models import LLMResponse, Message, ToolCall
from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult

NOISE = "SCAN-NOISE-LINE\n" * 4000


def _run(coro):
    return asyncio.run(coro)


class _NoisyParams(ToolParams):
    pass


class _NoisyTool(BaseTool):
    """A tool whose output is far larger than any report of it."""

    def __init__(self) -> None:
        super().__init__("read", "emit a lot of output")

    def get_params_class(self):
        return _NoisyParams

    def approval_scope(self, params):
        return ApprovalScope.exact_only()

    def validate_params(self, toolParams):
        return None

    async def run(self, toolParams) -> ToolResult:
        return ToolResult(success=True, output=NOISE)


def _provider(responses: List[LLMResponse]) -> MagicMock:
    provider = MagicMock()
    provider.model = "test"
    provider.complete = AsyncMock(side_effect=responses)
    provider.get_max_output_tokens = AsyncMock(return_value=2048)
    provider.get_max_context_tokens = AsyncMock(return_value=100_000)

    def fork(model=None):
        snapshot = copy(provider)
        snapshot.model = provider.model if model is None else model
        return snapshot

    provider.fork.side_effect = fork
    return provider


def _call(tool_name: str, parameters: dict, call_id: str = "c1") -> LLMResponse:
    return LLMResponse(
        content="",
        model="test",
        tool_calls=[
            ToolCall(tool_name=tool_name, parameters=parameters, call_id=call_id)
        ],
    )


class ContextOffloadTest(unittest.TestCase):
    def _master_history_after_delegation(
        self, collect_with_wait: bool
    ) -> List[Message]:
        agent_provider = _provider(
            [
                _call("read", {}),
                _call(
                    REPORT_TOOL_NAME,
                    {
                        "status": "completed",
                        "findings": "Apache on 10.0.0.5",
                        "next_steps": "",
                    },
                ),
            ]
        )
        master_responses = [
            _call(
                "delegate",
                {
                    "tasks": [
                        {
                            "agent_type": "recon",
                            "instruction": "enumerate 10.0.0.5",
                            "success_criteria": "",
                            "name": "host-enum",
                        }
                    ]
                },
            )
        ]
        if collect_with_wait:
            master_responses.append(_call("agent_wait", {"ids": ["a1"]}, "c2"))
        master_responses.append(
            LLMResponse(content="Understood.", model="test", tool_calls=None)
        )
        master_provider = _provider(master_responses)

        with tempfile.TemporaryDirectory() as d:
            registry = AgentRegistry(max_depth=2, max_total=10)
            supervisor = AgentSupervisor(registry)
            agent_manager = AgentManager(agents_dir=str(Path(d)))
            delegate = SpawnAgentTool(
                agent_manager=agent_manager,
                master_tools=[_NoisyTool()],
                provider_getter=lambda: agent_provider,
                registry=registry,
                supervisor=supervisor,
            )
            history: List[Message] = []
            session = LoopSession(
                provider=master_provider,
                history=history,
                tools=[delegate, AgentWaitTool(supervisor)],
                approval=WhitelistAutoApproval(),
                output=QuietSink(),
                max_iterations=10,
            )

            async def scenario():
                await ConversationEngine().run_loop(session)
                if not collect_with_wait:
                    # Nobody collected: the report is still owed to the master.
                    await supervisor.wait()
                await supervisor.cancel_all()

            _run(scenario())
        return history

    def test_agent_tool_output_never_enters_master_history(self):
        history = self._master_history_after_delegation(collect_with_wait=True)
        rendered = "\n".join(m.content or "" for m in history)
        self.assertNotIn("SCAN-NOISE-LINE", rendered)
        self.assertIn("Apache on 10.0.0.5", rendered)

    def test_master_context_growth_is_report_sized_not_agent_sized(self):
        history = self._master_history_after_delegation(collect_with_wait=True)
        grown = sum(len(m.content or "") for m in history)
        self.assertLess(grown, len(NOISE) // 10)

    def test_spawning_alone_costs_the_master_only_the_agent_ids(self):
        history = self._master_history_after_delegation(collect_with_wait=False)
        rendered = "\n".join(m.content or "" for m in history)
        self.assertNotIn("SCAN-NOISE-LINE", rendered)
        # The detached spawn result names the agent but carries no findings.
        self.assertIn("host-enum (a1, recon)", rendered)
        self.assertNotIn("Apache on 10.0.0.5", rendered)


if __name__ == "__main__":
    unittest.main()
