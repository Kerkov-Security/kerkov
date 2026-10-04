"""Tests for GeminiProvider._clean_schema and tool-call message round-tripping."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from google.ai import generativelanguage as glm

from kerkov.models.basic_models import Message
from kerkov.providers.gemini_provider import GeminiProvider


def _make_provider() -> GeminiProvider:
    return GeminiProvider(api_key="test-key", model="gemini-2.5-flash")


class ModelTokenLimitsTest(unittest.IsolatedAsyncioTestCase):
    """Both configured names and catalog resource names resolve model limits."""

    async def test_configured_model_name_resolves_and_caches_limits(self) -> None:
        provider = _make_provider()
        metadata = SimpleNamespace(input_token_limit=1_000_000, output_token_limit=8192)
        with patch("google.generativeai.get_model", return_value=metadata) as lookup:
            self.assertEqual(await provider.get_max_context_tokens(), 1_000_000)
            self.assertEqual(await provider.get_max_output_tokens(), 8192)
        lookup.assert_called_once_with(
            "models/gemini-2.5-flash", client=provider._model_client
        )

    async def test_selected_catalog_model_keeps_one_resource_prefix(self) -> None:
        provider = _make_provider()
        catalog_model = SimpleNamespace(name="models/gemini-catalog-model")
        metadata = SimpleNamespace(
            input_token_limit=2_000_000, output_token_limit=16384
        )
        with (
            patch("google.generativeai.list_models", return_value=[catalog_model]),
            patch("google.generativeai.get_model", return_value=metadata) as lookup,
        ):
            provider.set_model(provider.list_models()[0])
            self.assertEqual(await provider.get_max_context_tokens(), 2_000_000)
            self.assertEqual(await provider.get_max_output_tokens(), 16384)
        lookup.assert_called_once_with(
            catalog_model.name, client=provider._model_client
        )


class CredentialIsolationTest(unittest.IsolatedAsyncioTestCase):
    """Testing another key must never change a live provider's SDK clients."""

    async def test_failed_candidate_keeps_live_catalog_limits_and_completions(self):
        metadata = glm.Model(
            name="models/gemini-live", input_token_limit=32_000, output_token_limit=4096
        )
        live_catalog = Mock()
        live_catalog.list_models.return_value = [metadata]
        live_catalog.get_model.return_value = metadata
        candidate_catalog = Mock()
        candidate_catalog.list_models.side_effect = ValueError("invalid candidate")
        generation = Mock()
        response = glm.GenerateContentResponse(
            candidates=[
                glm.Candidate(content=glm.Content(parts=[glm.Part(text="still live")]))
            ]
        )
        generation.generate_content = AsyncMock(return_value=response)

        async def chunks():
            yield response

        generation.stream_generate_content = AsyncMock(
            side_effect=lambda *args: chunks()
        )
        with (
            patch("google.generativeai.configure") as configure,
            patch(
                "kerkov.providers.gemini_provider.glm.ModelServiceClient",
                side_effect=[live_catalog, candidate_catalog],
            ) as model_clients,
            patch(
                "kerkov.providers.gemini_provider.glm.GenerativeServiceAsyncClient",
                return_value=generation,
            ) as generation_clients,
        ):
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
            deltas = []
            self.assertEqual(
                (
                    await live.complete(
                        [Message(role="user", content="continue")],
                        on_delta=deltas.append,
                    )
                ).content,
                "still live",
            )
        configure.assert_not_called()
        self.assertEqual(
            [call.kwargs for call in model_clients.call_args_list],
            [
                {"client_options": {"api_key": "live-key"}},
                {"client_options": {"api_key": "candidate-key"}},
            ],
        )
        generation_clients.assert_called_once_with(
            client_options={"api_key": "live-key"}
        )
        self.assertEqual(deltas, ["still live"])
        generation.generate_content.assert_awaited_once()
        generation.stream_generate_content.assert_awaited_once()


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
