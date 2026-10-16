"""Usage follows requests, including retries and interrupted/failed runs."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kerkov.agents.agent_runner import AgentRunner
from kerkov.agents.models import AgentProfile
from kerkov.core.conversation_engine import (
    ConversationEngine,
    LoopSession,
    QuietSink,
    WhitelistAutoApproval,
)
from kerkov.models.basic_models import LLMResponse, Message, TokenUsage, ToolCall


def provider(responses):
    result = MagicMock()
    result.model = "small"
    result.get_model_info.return_value = {"provider": "test", "model": "small"}
    result.get_max_output_tokens = AsyncMock(return_value=1000)
    result.get_max_context_tokens = AsyncMock(return_value=100_000)
    result.complete = AsyncMock(side_effect=responses)
    result.caches_prompt_prefix = False
    return result


def session(model):
    return LoopSession(
        provider=model,
        history=[Message(role="user", content="Read supplied values")],
        tools=[],
        approval=WhitelistAutoApproval(),
        output=QuietSink(),
        max_iterations=3,
        engagement_store=MagicMock(),
    )


def response(text="done", **kwargs):
    return LLMResponse(
        content=text,
        model="small",
        usage=TokenUsage(prompt_tokens=100, completion_tokens=10, total_tokens=110),
        **kwargs,
    )


def test_request_recorded_before_tool_processing_fails():

    async def scenario():
        run = session(
            provider(
                [response("", tool_calls=[ToolCall(tool_name="bad", parameters={})])]
            )
        )
        engine = ConversationEngine()
        engine._handle_tool_calls = AsyncMock(side_effect=RuntimeError("tool failure"))
        with pytest.raises(RuntimeError, match="tool failure"):
            await engine.run_loop(run)
        call = run.engagement_store.record_model_call.call_args.kwargs
        assert call["usage"].prompt_tokens == 100
        assert call["status"] == "completed"
        assert call["provider"] == "test"
        assert call["model"] == "small"
        assert (
            run.engagement_store.record_run_outcome.call_args.kwargs["status"]
            == "failed"
        )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "error,status",
    [(RuntimeError("down"), "failed"), (asyncio.CancelledError(), "cancelled")],
)
def test_failed_or_cancelled_request_has_unknown_usage(error, status):

    async def scenario():
        run = session(provider([error]))
        with pytest.raises(type(error)):
            await ConversationEngine().run_loop(run)
        run.engagement_store.record_model_call.assert_called_once()
        call = run.engagement_store.record_model_call.call_args.kwargs
        assert call["usage"] is None
        assert call["status"] == status
        assert (
            run.engagement_store.record_run_outcome.call_args.kwargs["status"] == status
        )

    asyncio.run(scenario())


def test_empty_response_is_still_a_billable_call():

    async def scenario():
        run = session(provider([response("")]))
        await ConversationEngine().run_loop(run)
        run.engagement_store.record_model_call.assert_called_once()
        assert (
            run.engagement_store.record_model_call.call_args.kwargs[
                "usage"
            ].total_tokens
            == 110
        )

    asyncio.run(scenario())


def test_usage_keeps_the_model_selected_when_request_started():

    async def scenario():
        model = provider([])

        async def complete(**kwargs):
            model.model = "new-model"
            return response()

        model.complete.side_effect = complete
        run = session(model)
        await ConversationEngine().run_loop(run)
        assert (
            run.engagement_store.record_model_call.call_args.kwargs["model"] == "small"
        )

    asyncio.run(scenario())


@pytest.mark.parametrize("label", ["AnthropicProvider", "anthropic", "claude"])
def test_recorded_usage_uses_canonical_provider_identity(label):

    async def scenario():
        model = provider([response()])
        model.get_model_info.return_value["provider"] = label
        run = session(model)
        await ConversationEngine().run_loop(run)
        assert (
            run.engagement_store.record_model_call.call_args.kwargs["provider"]
            == "anthropic"
        )

    asyncio.run(scenario())


def test_retry_and_compaction_requests_are_counted_once_each():

    async def scenario():
        run = session(
            provider(
                [
                    response("I can do that if you want."),
                    response(),
                    response("summary"),
                ]
            )
        )
        run.transient_prompts = ["Work autonomously"]
        engine = ConversationEngine()
        await engine.run_loop(run)
        assert run.provider.complete.await_count == 2
        await engine._make_summarize_fn(run)("Summarize the supplied history")
        calls = run.engagement_store.record_model_call.call_args_list
        assert len(calls) == 3
        assert [call.kwargs["purpose"] for call in calls] == [
            "completion",
            "completion",
            "compaction",
        ]
        assert (
            run.engagement_store.record_run_outcome.call_args.kwargs["iterations"] == 2
        )

    asyncio.run(scenario())


def test_retry_blocked_by_iteration_cap_is_not_completed():

    async def scenario():
        run = session(provider([response("I can do that if you want.")]))
        run.transient_prompts = ["Work autonomously"]
        run.max_iterations = 1
        await ConversationEngine().run_loop(run)
        outcome = run.engagement_store.record_run_outcome.call_args.kwargs
        assert outcome["status"] == "stuck"
        assert outcome["iterations"] == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["completed", "partial", "stuck"])
def test_agent_records_one_outcome_with_report_status(status):

    async def scenario():
        store = MagicMock()
        model = provider(
            [
                response(
                    "",
                    tool_calls=[
                        ToolCall(
                            tool_name="report",
                            parameters={"status": status, "findings": "result"},
                        )
                    ],
                )
            ]
        )
        runner = AgentRunner(
            profile=AgentProfile(
                name="analysis",
                description="test",
                system_prompt="test",
                allowed_tools=[],
            ),
            instruction="Read the supplied values",
            provider=model,
            tools=[],
            engagement_store=store,
        )
        store.start_engagement.return_value = None
        await runner.run()
        store.record_model_call.assert_called_once()
        store.record_run_outcome.assert_called_once()
        outcome = store.record_run_outcome.call_args.kwargs
        assert outcome["status"] == status
        assert outcome["report_status"] == status
        assert outcome["iterations"] == 1

    asyncio.run(scenario())


def test_agent_provider_failure_keeps_a_terminal_outcome():

    async def scenario():
        store = MagicMock()
        store.start_engagement.return_value = None
        runner = AgentRunner(
            profile=AgentProfile(
                name="analysis",
                description="test",
                system_prompt="test",
                allowed_tools=[],
            ),
            instruction="Read the supplied values",
            provider=provider([RuntimeError("unavailable")]),
            tools=[],
            engagement_store=store,
        )
        with pytest.raises(RuntimeError, match="unavailable"):
            await runner.run()
        store.record_run_outcome.assert_called_once()
        assert store.record_run_outcome.call_args.kwargs["status"] == "failed"

    asyncio.run(scenario())
