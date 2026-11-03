"""Provider-neutral JSON Schema for tool parameters."""

import copy
from functools import lru_cache
from typing import Any, Dict, FrozenSet, Mapping, Tuple, Type

from pydantic import BaseModel

_DROP_KEYS = frozenset({"title", "$defs", "definitions", "$schema"})
# Keywords whose value is a name -> schema mapping, a schema, or a list of schemas.
# Everything else (enum, const, default, examples, required...) is data and is
# copied verbatim, so a `title` key inside a default value survives.
_SCHEMA_MAP_KEYS = frozenset({"properties", "patternProperties", "dependentSchemas"})
_SCHEMA_KEYS = frozenset(
    {
        "additionalProperties",
        "items",
        "contains",
        "not",
        "if",
        "then",
        "else",
        "propertyNames",
        "unevaluatedProperties",
        "unevaluatedItems",
    }
)
_SCHEMA_LIST_KEYS = frozenset({"anyOf", "oneOf", "allOf", "prefixItems"})


class RecursiveSchemaError(ValueError):
    """A tool's parameters reference themselves and cannot be inlined."""


def tool_parameters_schema(params_class: Type[BaseModel]) -> Dict[str, Any]:
    """Return a fresh copy of the normalized parameters schema for a params class."""
    return copy.deepcopy(_cached_schema(params_class))


@lru_cache(maxsize=None)
def _cached_schema(params_class: Type[BaseModel]) -> Dict[str, Any]:
    return normalize_schema(params_class.model_json_schema())


def normalize_schema(schema: Mapping[str, Any]) -> Dict[str, Any]:
    """Inline refs, collapse Optional unions and drop titles from a JSON Schema."""
    defs: Mapping[str, Any] = {
        **schema.get("definitions", {}),
        **schema.get("$defs", {}),
    }
    normalized = _normalize(schema, defs, frozenset())
    normalized.setdefault("type", "object")
    normalized.setdefault("properties", {})
    return normalized


def _normalize(
    node: Mapping[str, Any], defs: Mapping[str, Any], resolving: FrozenSet[str]
) -> Dict[str, Any]:
    if "$ref" in node:
        node, resolving = _inline_ref(node, defs, resolving)
    if "anyOf" in node:
        node = _collapse_optional(node)
        if "$ref" in node:
            node, resolving = _inline_ref(node, defs, resolving)

    result: Dict[str, Any] = {}
    for key, value in node.items():
        if key in _DROP_KEYS:
            continue
        if key in _SCHEMA_MAP_KEYS and isinstance(value, Mapping):
            result[key] = {
                name: _normalize_any(sub, defs, resolving)
                for name, sub in value.items()
            }
        elif key in _SCHEMA_KEYS:
            result[key] = _normalize_any(value, defs, resolving)
        elif key in _SCHEMA_LIST_KEYS and isinstance(value, list):
            result[key] = [_normalize_any(sub, defs, resolving) for sub in value]
        else:
            result[key] = copy.deepcopy(value)
    return result


def _normalize_any(
    value: Any, defs: Mapping[str, Any], resolving: FrozenSet[str]
) -> Any:
    if isinstance(value, Mapping):
        return _normalize(value, defs, resolving)
    return copy.deepcopy(value)


def _inline_ref(
    node: Mapping[str, Any], defs: Mapping[str, Any], resolving: FrozenSet[str]
) -> Tuple[Dict[str, Any], FrozenSet[str]]:
    ref = node["$ref"]
    name = ref.rsplit("/", 1)[-1]
    if name in resolving:
        raise RecursiveSchemaError(f"recursive schema reference: {ref}")
    if name not in defs:
        raise ValueError(f"unresolved schema reference: {ref}")
    merged = dict(defs[name])
    merged.update((key, value) for key, value in node.items() if key != "$ref")
    return merged, resolving | {name}


def _collapse_optional(node: Mapping[str, Any]) -> Mapping[str, Any]:
    """Turn `anyOf [T, null]` into `T`; omitting the field already means None."""
    branches = node["anyOf"]
    if not isinstance(branches, list):
        return node
    non_null = [
        branch
        for branch in branches
        if not (isinstance(branch, Mapping) and branch.get("type") == "null")
    ]
    if len(non_null) != 1 or len(non_null) == len(branches):
        return node
    if not isinstance(non_null[0], Mapping):
        return node

    collapsed = dict(non_null[0])
    for key, value in node.items():
        if key == "anyOf" or (key == "default" and value is None):
            continue
        collapsed[key] = value
    return collapsed
