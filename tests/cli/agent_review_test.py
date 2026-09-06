"""Tests for operator review of an attached agent's tool calls."""

import asyncio
import contextlib
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

from kerkov.agents.agent_manager import AgentManager
from kerkov.agents.agent_registry import AgentRegistry
from kerkov.agents.agent_session import AgentSession, AgentState
from kerkov.agents.agent_supervisor import AgentSupervisor
from kerkov.agents.agent_tool import SpawnAgentParams, SpawnAgentTool
from kerkov.agents.report_tool import REPORT_TOOL_NAME
from kerkov.cli.agent_focus_manager import AgentFocusManager
from kerkov.cli.agent_review import AttachedAgentReview
from kerkov.cli.approval_mode_manager import ApprovalMode
from kerkov.cli.tool_approval_helper import ToolApprovalResult
from kerkov.models.basic_models import LLMResponse, ToolCall
from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult


def _run(coro):
    return asyncio.run(coro)


def _create_mock_session(agent_id: str = "a1") -> AgentSession:
    session = AgentSession(
        id=agent_id,
        agent_run_id=f"run-{agent_id}",
        profile="recon",
        task="ping 10.0.0.5",
        name="google-pinger",
    )
    session.state = AgentState.RUNNING
    return session


def _create_mock_tool(name: str = "exec") -> MagicMock:
    tool = MagicMock()
    tool.name = name
    return tool


def _create_mock_helper(approved: bool = True) -> MagicMock:
    helper = MagicMock()
    helper.non_interactive_auto_approve = False

    async def _approve(tool, params, mode, *, requester=None):
        helper.seen = (tool.name, params, mode, requester)
        return ToolApprovalResult(approved=approved)

    helper.get_tool_usage_approval = MagicMock(side_effect=_approve)
    return helper


def _create_mock_input_manager() -> MagicMock:
    manager = MagicMock()
    manager.claimed = 0

    @contextlib.asynccontextmanager
    async def _takeover():
        manager.claimed += 1
        yield

    manager.takeover = _takeover
    return manager


def _create_gate(
    *,
    focus: AgentFocusManager,
    helper: MagicMock,
    input_manager: MagicMock,
) -> AttachedAgentReview:
    modes = MagicMock()
    modes.current = ApprovalMode.NORMAL
    output = MagicMock()
    output.suspend_status.return_value = contextlib.nullcontext()
    return AttachedAgentReview(focus, input_manager, helper, modes, output)


class ShouldReviewTest(unittest.TestCase):
    def test_off_by_default(self):
        session = _create_mock_session()
        focus = AgentFocusManager()
        focus.focus_agent(session.id)
        gate = _create_gate(
            focus=focus,
            helper=_create_mock_helper(),
            input_manager=_create_mock_input_manager(),
        )

        self.assertFalse(gate.should_review(session))

    def test_reviews_the_focused_agent_when_asked(self):
        session = _create_mock_session()
        session.review_tools = True
        focus = AgentFocusManager()
        focus.focus_agent(session.id)
        gate = _create_gate(
            focus=focus,
            helper=_create_mock_helper(),
            input_manager=_create_mock_input_manager(),
        )

        self.assertTrue(gate.should_review(session))

    def test_a_detached_agent_never_prompts(self):
        """Nobody is watching it, so the question would hang the run."""
        session = _create_mock_session()
        session.review_tools = True
        other = _create_mock_session("a2")
        focus = AgentFocusManager()
        focus.focus_agent(other.id)
        gate = _create_gate(
            focus=focus,
            helper=_create_mock_helper(),
            input_manager=_create_mock_input_manager(),
        )

        self.assertFalse(gate.should_review(session))

    def test_master_focus_means_no_review(self):
        session = _create_mock_session()
        session.review_tools = True
        gate = _create_gate(
            focus=AgentFocusManager(),
            helper=_create_mock_helper(),
            input_manager=_create_mock_input_manager(),
        )

        self.assertFalse(gate.should_review(session))

    def test_non_interactive_run_never_prompts(self):
        session = _create_mock_session()
        session.review_tools = True
        focus = AgentFocusManager()
        focus.focus_agent(session.id)
        helper = _create_mock_helper()
        helper.non_interactive_auto_approve = True
        gate = _create_gate(
            focus=focus,
            helper=helper,
            input_manager=_create_mock_input_manager(),
        )

        self.assertFalse(gate.should_review(session))


class ReviewTest(unittest.TestCase):
    def test_takes_the_terminal_and_names_the_requester(self):
        session = _create_mock_session()
        focus = AgentFocusManager()
        focus.focus_agent(session.id)
        helper = _create_mock_helper()
        input_manager = _create_mock_input_manager()
        gate = _create_gate(focus=focus, helper=helper, input_manager=input_manager)

        result = _run(gate.review(session, _create_mock_tool("http"), {"url": "u"}))

        self.assertTrue(result.approved)
        self.assertEqual(input_manager.claimed, 1)
        self.assertEqual(
            helper.seen,
            ("http", {"url": "u"}, ApprovalMode.NORMAL, "google-pinger"),
        )

    def test_the_verdict_lands_in_the_agents_own_buffer(self):
        """So it shows up in the attached stream and survives a re-attach."""
        session = _create_mock_session()
        focus = AgentFocusManager()
        focus.focus_agent(session.id)
        gate = _create_gate(
            focus=focus,
            helper=_create_mock_helper(approved=False),
            input_manager=_create_mock_input_manager(),
        )

        _run(gate.review(session, _create_mock_tool("exec"), {}))

        self.assertIn("waiting for approval: exec", session.events.buffer)
        self.assertIn("denied: exec", session.events.buffer)


class _GatedParams(ToolParams):
    pass


class _GatedTool(BaseTool):
    """A tool the agent is allowed to use but the operator may want to see."""

    def __init__(self) -> None:
        super().__init__("read", "gated")
        self.calls = 0

    def get_params_class(self):
        return _GatedParams

    def validate_params(self, toolParams):
        return None

    async def run(self, toolParams) -> ToolResult:
        self.calls += 1
        return ToolResult(success=True, output="ok")


def _create_mock_provider() -> MagicMock:
    """Each agent calls the gated tool once, then reports.

    Keyed on the history it is handed rather than a call counter: concurrent
    agents share this provider, so a fixed sequence would interleave.
    """
    provider = MagicMock()
    provider.model = "test-model"
    provider.get_max_output_tokens = AsyncMock(return_value=2048)
    provider.get_max_context_tokens = AsyncMock(return_value=100_000)

    async def _complete(messages, **kwargs):
        if any(m.role == "tool" for m in messages):
            return LLMResponse(
                content="",
                model="test",
                tool_calls=[
                    ToolCall(
                        tool_name=REPORT_TOOL_NAME,
                        parameters={
                            "status": "completed",
                            "findings": "done",
                            "next_steps": "",
                        },
                        call_id="r1",
                    )
                ],
            )
        return LLMResponse(
            content="",
            model="test",
            tool_calls=[ToolCall(tool_name="read", parameters={}, call_id="c1")],
        )

    provider.complete = AsyncMock(side_effect=_complete)
    return provider


class ReviewInAFullRunTest(unittest.TestCase):
    """The end-to-end shape: only the agent on screen asks before it acts."""

    def test_the_attached_agent_is_asked_and_the_detached_one_is_not(self):
        # The helper is a stub, so `report` is reviewed here too; in a real run
        # the rules engine allows it before the box is ever built.
        focus = AgentFocusManager()
        helper = _create_mock_helper()
        gate = _create_gate(
            focus=focus,
            helper=helper,
            input_manager=_create_mock_input_manager(),
        )
        gated = _GatedTool()

        registry = AgentRegistry(max_depth=2, max_total=10)
        supervisor = AgentSupervisor(registry)
        with tempfile.TemporaryDirectory() as agents_dir:
            manager = AgentManager(agents_dir=agents_dir)
        spawn = SpawnAgentTool(
            agent_manager=manager,
            master_tools=[gated],
            provider_getter=_create_mock_provider,
            registry=registry,
            supervisor=supervisor,
            review_gate=gate,
        )

        async def _scenario():
            await spawn.run(
                SpawnAgentParams(
                    tasks=[
                        {
                            "agent_type": "recon",
                            "instruction": "watched",
                            "name": "watched",
                        },
                        {
                            "agent_type": "recon",
                            "instruction": "detached",
                            "name": "detached",
                        },
                    ]
                )
            )
            watched, detached = supervisor.get("a1"), supervisor.get("a2")
            watched.review_tools = True
            detached.review_tools = True
            focus.focus_agent(watched.id)
            await supervisor.wait()
            return watched, detached

        watched, detached = asyncio.run(_scenario())

        # Both ran their gated call; only the one on screen was put to the user.
        self.assertEqual(gated.calls, 2)
        asked = helper.get_tool_usage_approval.call_args_list
        self.assertIn("read", [call.args[0].name for call in asked])
        self.assertEqual({call.kwargs["requester"] for call in asked}, {"watched"})
        self.assertIn("waiting for approval: read", watched.events.buffer)
        self.assertNotIn("waiting for approval: read", detached.events.buffer)


if __name__ == "__main__":
    unittest.main()
