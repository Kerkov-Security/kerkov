"""Tests for the provider-neutral tool parameter schema."""

from typing import Any, Dict, Literal, Optional, Union

import pytest
from pydantic import BaseModel, Field

from kerkov.tools.base_tool import ToolParams
from kerkov.tools.schema import (
    RecursiveSchemaError,
    normalize_schema,
    tool_parameters_schema,
)


class _Item(BaseModel):
    title: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list, max_length=3)


class _Params(ToolParams):
    title: str = Field(description="Finding title", pattern="^[a-z]+$")
    items: Optional[list[_Item]] = Field(None, description="Optional item list")
    headers: Optional[Dict[str, str]] = None
    action: Literal["get"] = "get"
    refresh: Optional[bool] = Field(False, description="Bypass cache")
    count: int = Field(5, ge=1, le=10)
    choice: Union[str, int, None] = None
    payload: Dict[str, Any] = Field(default_factory=lambda: {"title": "kept"})


class _Node(ToolParams):
    child: Optional["_Node"] = None


def _keys(node: Any) -> set:
    found: set = set()
    if isinstance(node, dict):
        for key, value in node.items():
            found.add(key)
            found |= _keys(value)
    elif isinstance(node, list):
        for value in node:
            found |= _keys(value)
    return found


def test_drops_titles_but_keeps_title_properties() -> None:
    schema = tool_parameters_schema(_Params)

    assert "title" not in schema
    assert schema["properties"]["title"] == {
        "type": "string",
        "description": "Finding title",
        "pattern": "^[a-z]+$",
    }
    assert "title" in schema["required"]
    item = schema["properties"]["items"]["items"]
    assert item["properties"]["title"] == {"type": "string", "minLength": 1}
    assert "title" not in item


def test_inlines_refs_inside_optional_arrays() -> None:
    schema = tool_parameters_schema(_Params)

    assert {"$ref", "$defs"}.isdisjoint(_keys(schema))
    assert schema["properties"]["items"] == {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "minLength": 1},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 3,
                },
            },
            "required": ["title"],
        },
        "description": "Optional item list",
    }


def test_collapses_optional_and_drops_only_null_defaults() -> None:
    properties = tool_parameters_schema(_Params)["properties"]

    assert properties["headers"] == {
        "type": "object",
        "additionalProperties": {"type": "string"},
    }
    assert properties["refresh"] == {
        "type": "boolean",
        "default": False,
        "description": "Bypass cache",
    }


def test_keeps_validation_keywords_and_multi_type_unions() -> None:
    properties = tool_parameters_schema(_Params)["properties"]

    assert properties["action"]["const"] == "get"
    assert properties["count"] == {
        "type": "integer",
        "default": 5,
        "minimum": 1,
        "maximum": 10,
    }
    assert properties["choice"]["anyOf"] == [
        {"type": "string"},
        {"type": "integer"},
        {"type": "null"},
    ]


def test_data_keywords_are_copied_verbatim() -> None:
    schema = normalize_schema(
        {
            "type": "object",
            "properties": {
                "mode": {"enum": ["title", "body"], "default": {"title": "x"}},
            },
        }
    )

    assert schema["properties"]["mode"] == {
        "enum": ["title", "body"],
        "default": {"title": "x"},
    }


def test_recursive_model_fails_loudly() -> None:
    with pytest.raises(RecursiveSchemaError):
        tool_parameters_schema(_Node)


def test_unresolved_reference_fails_loudly() -> None:
    with pytest.raises(ValueError, match="unresolved"):
        normalize_schema({"properties": {"x": {"$ref": "#/$defs/Missing"}}})


def test_returns_fresh_copies_of_a_cached_schema() -> None:
    first = tool_parameters_schema(_Params)
    first["properties"]["title"]["mutated"] = True

    second = tool_parameters_schema(_Params)

    assert "mutated" not in second["properties"]["title"]
    assert second == tool_parameters_schema(_Params)
