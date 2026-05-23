"""Tests for GeminiProvider._clean_schema."""

import unittest
from unittest.mock import patch

from src.providers.gemini_provider import GeminiProvider


def _make_provider() -> GeminiProvider:
    with patch("google.generativeai.configure"):
        return GeminiProvider(api_key="test-key", model="gemini-2.5-flash")


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
        self.assertTrue(result.get("nullable"), "Expected nullable=True for Optional field")
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
        defs = {"Widget": {"type": "object", "properties": {"name": {"type": "string"}}}}
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
        prop_data = {"anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "boolean"}]}

        with self.assertRaises(ValueError):
            self.provider._clean_schema(prop_data, {})

    def test_single_non_null_branch_does_not_raise(self) -> None:
        prop_data = {"anyOf": [{"type": "string"}, {"type": "null"}]}

        result = self.provider._clean_schema(prop_data, {})

        self.assertEqual(result["type"], "string")
        self.assertTrue(result.get("nullable"))


if __name__ == "__main__":
    unittest.main()
