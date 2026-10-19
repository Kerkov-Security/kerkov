"""Tests for GeminiProvider._clean_schema and tool-call message round-tripping."""

import asyncio
import json
import unittest
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import AsyncMock, Mock, patch

import pytest

from kerkov.core.engagement.engagement_store import EngagementStore
from kerkov.models.basic_models import Message
from kerkov.providers.gemini_provider import GeminiProvider
from kerkov.routing.measurement import summarize_engagement


def _make_provider() -> GeminiProvider:
    return GeminiProvider(api_key="test-key", model="gemini-2.5-flash")


async def _aiter(items: List[Any]):
    for item in items:
        yield item


class ModelTokenLimitsTest(unittest.IsolatedAsyncioTestCase):
    """Both configured names and catalog resource names resolve model limits."""

    async def test_configured_model_name_resolves_and_caches_limits(self) -> None:
        provider = _make_provider()
        metadata = SimpleNamespace(input_token_limit=1_000_000, output_token_limit=8192)
        with patch.object(
            provider.client.models, "get", return_value=metadata
        ) as lookup:
            self.assertEqual(await provider.get_max_context_tokens(), 1_000_000)
            self.assertEqual(await provider.get_max_output_tokens(), 8192)
        lookup.assert_called_once_with(model="models/gemini-2.5-flash")

    async def test_selected_catalog_model_keeps_one_resource_prefix(self) -> None:
        provider = _make_provider()
        catalog_model = SimpleNamespace(
            name="models/gemini-catalog-model",
            supported_actions=["generateContent"],
        )
        metadata = SimpleNamespace(
            input_token_limit=2_000_000, output_token_limit=16384
        )
        with (
            patch.object(provider.client.models, "list", return_value=[catalog_model]),
            patch.object(
                provider.client.models, "get", return_value=metadata
            ) as lookup,
        ):
            provider.set_model(provider.list_models()[0])
            self.assertEqual(await provider.get_max_context_tokens(), 2_000_000)
            self.assertEqual(await provider.get_max_output_tokens(), 16384)
        lookup.assert_called_once_with(model="models/gemini-catalog-model")


class CredentialIsolationTest(unittest.IsolatedAsyncioTestCase):
    """Testing another key must never change a live provider's SDK client."""

    async def test_failed_candidate_keeps_live_catalog_limits_and_completions(self):
        metadata = SimpleNamespace(
            name="models/gemini-live",
            input_token_limit=32_000,
            output_token_limit=4096,
            supported_actions=["generateContent"],
        )
        response = SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(
                        parts=[SimpleNamespace(text="still live", function_call=None)]
                    )
                )
            ],
            usage_metadata=None,
        )

        live_client = Mock()
        live_client.models.list.return_value = [metadata]
        live_client.models.get.return_value = metadata
        live_client.aio.models.generate_content = AsyncMock(return_value=response)
        live_client.aio.models.generate_content_stream = AsyncMock(
            side_effect=lambda *a, **k: _aiter([response])
        )
        candidate_client = Mock()
        candidate_client.models.list.side_effect = ValueError("invalid candidate")

        with patch(
            "kerkov.providers.gemini_provider.genai.Client",
            side_effect=[live_client, candidate_client],
        ) as clients:
            live = GeminiProvider("live-key", "gemini-live")
            candidate = GeminiProvider("candidate-key", "gemini-candidate")
            self.assertFalse(candidate.validate_connection())
            self.assertTrue(live.validate_connection())
            self.assertEqual(live.list_models(), ["models/gemini-live"])
            self.assertEqual(await live.get_max_context_tokens(), 32_000)
            self.assertEqual(await live.get_max_output_tokens(), 4096)
            self.assertEqual(
                (
                    await live.complete([Message(role="user", content="continue")])
                ).content,
                "still live",
            )
            deltas: List[str] = []
            self.assertEqual(
                (
                    await live.complete(
                        [Message(role="user", content="continue")],
                        on_delta=deltas.append,
                    )
                ).content,
                "still live",
            )

        self.assertEqual(
            [call.kwargs for call in clients.call_args_list],
            [{"api_key": "live-key"}, {"api_key": "candidate-key"}],
        )
        self.assertEqual(deltas, ["still live"])
        live_client.aio.models.generate_content.assert_awaited_once()
        live_client.aio.models.generate_content_stream.assert_awaited_once()
        candidate_client.aio.models.generate_content.assert_not_called()


class ModelCatalogTest(unittest.TestCase):
    def test_only_generate_content_models_are_selectable(self) -> None:
        catalog = [
            SimpleNamespace(
                name="models/embedding-only",
                supported_actions=["embedContent", "batchEmbedContents"],
            ),
            SimpleNamespace(
                name="models/gemini-chat",
                supported_actions=["generateContent", "countTokens"],
            ),
            SimpleNamespace(
                name="models/legacy-chat",
                supported_actions=["generateMessage"],
            ),
            SimpleNamespace(
                name="models/another-chat",
                supported_actions=["generateContent"],
            ),
            SimpleNamespace(
                name="models/token-counter",
                supported_actions=["countTokens"],
            ),
        ]
        provider = _make_provider()
        with patch.object(provider.client.models, "list", return_value=iter(catalog)):
            self.assertEqual(
                provider.list_models(),
                ["models/gemini-chat", "models/another-chat"],
            )

    def test_unknown_capabilities_and_unnamed_models_are_excluded(self) -> None:
        catalog = [
            SimpleNamespace(name="models/missing-capabilities"),
            SimpleNamespace(name="models/no-capabilities", supported_actions=[]),
            SimpleNamespace(name="models/null-capabilities", supported_actions=None),
            SimpleNamespace(supported_actions=["generateContent"]),
        ]
        provider = _make_provider()
        with patch.object(provider.client.models, "list", return_value=iter(catalog)):
            self.assertEqual(provider.list_models(), [])


@pytest.mark.parametrize("streaming", [False, True])
def test_thinking_and_cached_usage_can_be_priced(tmp_path, streaming):
    metadata = SimpleNamespace(
        prompt_token_count=100,
        candidates_token_count=20,
        total_token_count=150,
        cached_content_token_count=60,
        thoughts_token_count=30,
    )
    provider = _make_provider()
    candidate = SimpleNamespace(
        content=SimpleNamespace(
            parts=[SimpleNamespace(text="Answer", function_call=None)]
        )
    )
    final = SimpleNamespace(candidates=[candidate], usage_metadata=metadata)

    async def chunks():
        yield SimpleNamespace(candidates=[], usage_metadata=None)
        yield final

    deltas: list = []
    if streaming:
        provider.client.aio.models.generate_content_stream = AsyncMock(
            side_effect=lambda *a, **k: chunks()
        )
    else:
        provider.client.aio.models.generate_content = AsyncMock(return_value=final)

    response = asyncio.run(
        provider.complete(
            [Message(role="user", content="Question")],
            on_delta=deltas.append if streaming else None,
        )
    )

    assert response.content == "Answer"
    assert deltas == (["Answer"] if streaming else [])
    assert response.usage.model_dump() == {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "cache_read_tokens": 60,
        "cache_creation_tokens": 0,
    }
    store = EngagementStore(tmp_path)
    store.start_engagement(
        mode="audit",
        model=provider.model,
        verbosity="standard",
        approval_mode="normal",
        provider="GeminiProvider",
    )
    store.record_model_call(
        provider="GeminiProvider",
        model=provider.model,
        usage=response.usage,
        elapsed_seconds=1,
        status="completed",
    )
    totals = summarize_engagement(
        tmp_path,
        store.engagement_id,
        {
            f"GeminiProvider/{provider.model}": {
                "input": 10,
                "output": 20,
                "cache_read": 1,
            }
        },
    )["totals"]
    assert totals["estimated_cost_usd"] == pytest.approx(0.00146)
    assert totals["unpriced_calls"] == 0


def test_explicit_thought_count_preserves_unmatched_provider_total():
    usage = GeminiProvider._usage_from(
        SimpleNamespace(
            usage_metadata=SimpleNamespace(
                prompt_token_count=100,
                candidates_token_count=20,
                thoughts_token_count=30,
                total_token_count=160,
            )
        )
    )

    assert usage.prompt_tokens == 100
    assert usage.completion_tokens == 50
    assert usage.total_tokens == 160
    assert usage.cache_read_tokens == 0


def test_usage_without_thinking_keeps_candidate_count():
    usage = GeminiProvider._usage_from(
        SimpleNamespace(
            usage_metadata=SimpleNamespace(
                prompt_token_count=100,
                candidates_token_count=20,
                total_token_count=120,
                thoughts_token_count=None,
            )
        )
    )

    assert usage.completion_tokens == 20
    assert usage.total_tokens == 120
    assert usage.cache_read_tokens == 0


class CleanSchemaNullableRefTest(unittest.TestCase):
    """Regression: Optional[NestedModel] emitted as anyOf[$ref, null] must resolve to object."""

    def setUp(self) -> None:
        self.provider = _make_provider()

    def test_nullable_ref_resolves_to_object_schema(self) -> None:
        defs = {
            "Thing": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            }
        }
        prop_data = {
            "description": "maybe thing",
            "anyOf": [
                {"$ref": "#/$defs/Thing"},
                {"type": "null"},
            ],
        }

        result = self.provider._clean_schema(prop_data, defs)

        self.assertEqual(result["type"], "object")
        self.assertTrue(
            result.get("nullable"), "Expected nullable=True for Optional field"
        )
        self.assertIn("properties", result)
        self.assertIn("value", result["properties"])

    def test_nullable_ref_preserves_outer_description(self) -> None:
        defs = {"Item": {"type": "object", "properties": {"x": {"type": "integer"}}}}
        prop_data = {
            "description": "outer desc",
            "anyOf": [{"$ref": "#/$defs/Item"}, {"type": "null"}],
        }

        result = self.provider._clean_schema(prop_data, defs)

        self.assertEqual(result["description"], "outer desc")

    def test_non_nullable_ref_resolves_to_object_schema(self) -> None:
        defs = {
            "Widget": {"type": "object", "properties": {"name": {"type": "string"}}}
        }
        prop_data = {"anyOf": [{"$ref": "#/$defs/Widget"}]}

        result = self.provider._clean_schema(prop_data, defs)

        self.assertEqual(result["type"], "object")
        self.assertNotIn("nullable", result)


class CleanSchemaUnionRejectionTest(unittest.TestCase):
    """Regression: multi-type non-null unions must raise instead of silently dropping branches."""

    def setUp(self) -> None:
        self.provider = _make_provider()

    def test_string_integer_union_raises(self) -> None:
        prop_data = {"anyOf": [{"type": "string"}, {"type": "integer"}]}

        with self.assertRaises(ValueError):
            self.provider._clean_schema(prop_data, {})

    def test_three_way_union_raises(self) -> None:
        prop_data = {
            "anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "boolean"}]
        }

        with self.assertRaises(ValueError):
            self.provider._clean_schema(prop_data, {})

    def test_single_non_null_branch_does_not_raise(self) -> None:
        prop_data = {"anyOf": [{"type": "string"}, {"type": "null"}]}

        result = self.provider._clean_schema(prop_data, {})

        self.assertEqual(result["type"], "string")
        self.assertTrue(result.get("nullable"))


class ConvertMessagesToolCallTest(unittest.TestCase):
    """Regression: assistant tool calls must round-trip as function_call /
    function_response parts, not collapse into empty 'model' turns (issue: the
    model re-issued the same tool call forever because it never saw it had
    already acted)."""

    def setUp(self) -> None:
        self.provider = _make_provider()

    def _tool_call_turn(self):
        """Mimic what the engine stores for a Gemini assistant tool-call turn."""
        return [
            Message(role="user", content="Please import sys"),
            Message(
                role="assistant",
                content="",
                raw_blocks=[
                    {
                        "type": "function_call",
                        "id": "call_abc123",
                        "name": "process",
                        "args": {"action": "paste", "text": "import sys"},
                    }
                ],
            ),
            Message(
                role="tool",
                content="Pasted 10 chars to session 054b7173.",
                call_id="call_abc123",
            ),
        ]

    def test_assistant_call_becomes_function_call_part(self) -> None:
        result = self.provider._convert_messages(self._tool_call_turn())

        model_turn = result[1]
        self.assertEqual(model_turn.role, "model")
        self.assertEqual(len(model_turn.parts), 1)
        fc = model_turn.parts[0].function_call
        self.assertEqual(fc.name, "process")
        self.assertEqual(dict(fc.args)["action"], "paste")

    def test_tool_result_becomes_function_response_part(self) -> None:
        result = self.provider._convert_messages(self._tool_call_turn())

        tool_turn = result[2]
        self.assertEqual(tool_turn.role, "user")
        fr = tool_turn.parts[0].function_response
        # Name must match the originating call so Gemini can pair them.
        self.assertEqual(fr.name, "process")
        self.assertEqual(
            dict(fr.response)["result"], "Pasted 10 chars to session 054b7173."
        )

    def test_assistant_turn_is_not_empty(self) -> None:
        """The original bug: the model turn was an empty-text part."""
        result = self.provider._convert_messages(self._tool_call_turn())

        model_turn = result[1]
        part = model_turn.parts[0]
        # A function_call part, not a stray empty text part.
        self.assertFalse(getattr(part, "text", ""))
        self.assertTrue(part.function_call.name)

    def test_consecutive_tool_results_coalesce_into_one_user_turn(self) -> None:
        messages = [
            Message(role="user", content="do two things"),
            Message(
                role="assistant",
                content="",
                raw_blocks=[
                    {
                        "type": "function_call",
                        "id": "c1",
                        "name": "process",
                        "args": {},
                    },
                    {"type": "function_call", "id": "c2", "name": "exec", "args": {}},
                ],
            ),
            Message(role="tool", content="r1", call_id="c1"),
            Message(role="tool", content="r2", call_id="c2"),
        ]

        result = self.provider._convert_messages(messages)

        # user, model(2 calls), user(2 responses) — not two separate user turns.
        self.assertEqual(len(result), 3)
        responses = result[2].parts
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0].function_response.name, "process")
        self.assertEqual(responses[1].function_response.name, "exec")

    def test_unmatched_tool_result_falls_back_to_text(self) -> None:
        messages = [
            Message(role="assistant", content="hi", raw_blocks=None),
            Message(role="tool", content="orphan result", call_id="missing"),
        ]

        result = self.provider._convert_messages(messages)

        tool_turn = result[1]
        self.assertEqual(tool_turn.role, "user")
        self.assertIn("orphan result", tool_turn.parts[0].text)

    def test_plain_text_assistant_turn_preserved(self) -> None:
        messages = [
            Message(role="user", content="hello"),
            Message(role="assistant", content="hi there", raw_blocks=None),
        ]

        result = self.provider._convert_messages(messages)

        self.assertEqual(result[1].role, "model")
        self.assertEqual(result[1].parts[0].text, "hi there")

    def test_raw_blocks_are_json_serializable(self) -> None:
        """raw_blocks must survive engagement-store persistence (json.dumps)."""
        blocks = [
            {
                "type": "function_call",
                "id": "call_x",
                "name": "process",
                "args": {
                    "action": "paste",
                    "n": 3,
                    "flag": True,
                    "nested": {"a": [1, 2]},
                },
            }
        ]
        # Should not raise.
        json.dumps(blocks)


if __name__ == "__main__":
    unittest.main()
