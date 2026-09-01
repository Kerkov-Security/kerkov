"""Operator input injected into one running agent (issue #177 stage 3).

The contract has two halves. Mechanically, queued text is drained at the top of
an iteration — the only point where history is provider-valid for an insert,
because every `tool_use` already has its result. In terms of context, it lands
in that agent's history and its own JSONL and nowhere else: not in a sibling's
history, and never in the master's (the C5 guarantee).
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock

from kerkov.agents.agent_runner import AgentRunner
from kerkov.agents.agent_session import AgentSession
from kerkov.agents.models import AgentProfile
from kerkov.agents.report_tool import REPORT_TOOL_NAME
from kerkov.cli.engagement_store import EngagementStore
from kerkov.core.conversation_engine import (
    OPERATOR_MESSAGE_PREFIX,
    ConversationEngine,
    LoopSession,
    QuietSink,
    WhitelistAutoApproval,
)
from kerkov.models.basic_models import LLMResponse, Message, ToolCall


def _run(coro):
    return asyncio.run(coro)


def _create_mock_provider(responses: List[LLMResponse]) -> MagicMock:
    provider = MagicMock()
    provider.model = "test-model"
    provider.complete = AsyncMock(side_effect=responses)
    provider.get_max_output_tokens = AsyncMock(return_value=2048)
    provider.get_max_context_tokens = AsyncMock(return_value=100_000)
    return provider


def _create_mock_session(agent_id: str = "a1") -> AgentSession:
    return AgentSession(
        id=agent_id,
        agent_run_id=f"run-{agent_id}",
        profile="recon",
        task="enumerate 10.0.0.5",
        name=agent_id,
    )


def _create_mock_profile() -> AgentProfile:
    return AgentProfile(
        name="recon",
        description="A test profile",
        system_prompt="You are a test agent.",
        allowed_tools=[],
        max_iterations=5,
    )


def _text(content: str) -> LLMResponse:
    return LLMResponse(content=content, model="test-model", tool_calls=None)


def _report(findings: str) -> LLMResponse:
    return LLMResponse(
        content="",
        model="test-model",
        tool_calls=[
            ToolCall(
                tool_name=REPORT_TOOL_NAME,
                parameters={
                    "status": "completed",
                    "findings": findings,
                    "next_steps": "",
                },
                call_id="c1",
            )
        ],
    )


def _loop_session(
    provider: MagicMock,
    history: List[Message],
    inbox: Optional["asyncio.Queue[str]"] = None,
    **kwargs,
) -> LoopSession:
    return LoopSession(
        provider=provider,
        history=history,
        tools=[],
        approval=WhitelistAutoApproval(),
        output=QuietSink(),
        max_iterations=5,
        inbox=inbox,
        **kwargs,
    )


class DrainTest(unittest.TestCase):
    """The engine half: what the drain puts into history, and when."""

    def test_a_queued_message_is_in_the_history_the_provider_is_handed(self):
        """The drain must precede the completion or the agent reads it a turn late."""
        provider = _create_mock_provider([_text("ok")])
        inbox: "asyncio.Queue[str]" = asyncio.Queue()
        inbox.put_nowait("stop scanning, pivot to 10.0.0.9")
        history: List[Message] = []

        _run(ConversationEngine().run_loop(_loop_session(provider, history, inbox)))

        handed = provider.complete.await_args.kwargs["messages"]
        rendered = "\n".join(m.content or "" for m in handed)
        self.assertIn("pivot to 10.0.0.9", rendered)

    def test_the_injected_message_carries_the_operator_label(self):
        """Unlabelled, the agent reads it as a restatement of its own task."""
        provider = _create_mock_provider([_text("ok")])
        inbox: "asyncio.Queue[str]" = asyncio.Queue()
        inbox.put_nowait("pivot to 10.0.0.9")
        history: List[Message] = []

        _run(ConversationEngine().run_loop(_loop_session(provider, history, inbox)))

        injected = [m for m in history if m.role == "user"]
        self.assertEqual(len(injected), 1)
        self.assertTrue(injected[0].content.startswith(OPERATOR_MESSAGE_PREFIX))

    def test_several_queued_messages_arrive_in_order_in_one_drain(self):
        provider = _create_mock_provider([_text("ok")])
        inbox: "asyncio.Queue[str]" = asyncio.Queue()
        for text in ("first", "second", "third"):
            inbox.put_nowait(text)
        history: List[Message] = []

        _run(ConversationEngine().run_loop(_loop_session(provider, history, inbox)))

        injected = [
            m.content.replace(OPERATOR_MESSAGE_PREFIX, "")
            for m in history
            if m.role == "user"
        ]
        self.assertEqual(injected, ["first", "second", "third"])

    def test_an_empty_inbox_is_a_no_op(self):
        provider = _create_mock_provider([_text("ok")])
        history: List[Message] = []

        _run(
            ConversationEngine().run_loop(
                _loop_session(provider, history, asyncio.Queue())
            )
        )

        self.assertEqual([m.role for m in history], ["assistant"])

    def test_no_inbox_leaves_the_loop_exactly_as_it_was(self):
        """Master and web sessions pass no inbox; nothing about them changes."""
        provider = _create_mock_provider([_text("ok")])
        history: List[Message] = []

        _run(ConversationEngine().run_loop(_loop_session(provider, history, None)))

        self.assertEqual([m.role for m in history], ["assistant"])


class AgentInjectionTest(unittest.TestCase):
    """The runner half: whose transcript the injected line ends up in."""

    def _run_agent_with_inbox(self, queued: Optional[str]):
        session = _create_mock_session("a1")
        sibling = _create_mock_session("a2")
        if queued is not None:
            session.inbox.put_nowait(queued)

        with tempfile.TemporaryDirectory() as d:
            store = EngagementStore(store_dir=Path(d) / "engagements")
            runner = AgentRunner(
                profile=_create_mock_profile(),
                instruction="enumerate 10.0.0.5",
                provider=_create_mock_provider([_report("Apache on 10.0.0.5")]),
                tools=[],
                engagement_store=store,
                session=session,
            )
            report = _run(runner.run())
            jsonl = (
                Path(d) / "engagements" / f"{report.engagement_id}.jsonl"
            ).read_text()
        return session, sibling, jsonl

    def test_the_message_lands_in_that_agents_history_and_its_jsonl(self):
        session, _, jsonl = self._run_agent_with_inbox("pivot to 10.0.0.9")

        rendered = "\n".join(m.content or "" for m in session.history)
        self.assertIn("pivot to 10.0.0.9", rendered)
        persisted = [json.loads(line) for line in jsonl.splitlines()[1:]]
        self.assertTrue(
            any("pivot to 10.0.0.9" in (row.get("content") or "") for row in persisted)
        )

    def test_it_reaches_no_sibling_agent(self):
        _, sibling, _ = self._run_agent_with_inbox("pivot to 10.0.0.9")

        self.assertEqual(sibling.inbox.qsize(), 0)
        self.assertEqual(sibling.history, [])

    def test_the_inbox_is_consumed_not_merely_read(self):
        session, _, _ = self._run_agent_with_inbox("pivot to 10.0.0.9")

        self.assertEqual(session.inbox.qsize(), 0)


if __name__ == "__main__":
    unittest.main()
