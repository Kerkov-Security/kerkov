"""Provider-neutral JSON Schema for tool parameters."""

import copy
from functools import lru_cache
from typing import Any, Callable, Dict, FrozenSet, Mapping, Tuple, Type

from pydantic import BaseModel

_DROP_KEYS = frozenset({"title", "$defs", "definitions", "$schema"})
_DEFINITION_TABLES = ("$defs", "definitions")
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
    defs: Dict[str, Mapping[str, Any]] = {
        table: schema.get(table) or {} for table in _DEFINITION_TABLES
    }
    normalized = _normalize(schema, defs, frozenset())
    normalized.setdefault("type", "object")
    normalized.setdefault("properties", {})
    return normalized


def _normalize(
    node: Mapping[str, Any],
    defs: Mapping[str, Mapping[str, Any]],
    resolving: FrozenSet[str],
) -> Dict[str, Any]:
    if "$ref" in node:
        node, resolving = _inline_ref(node, defs, resolving)
    if "anyOf" in node:
        node = _collapse_optional(node)
        if "$ref" in node:
            node, resolving = _inline_ref(node, defs, resolving)

    kept = {key: value for key, value in node.items() if key not in _DROP_KEYS}
    return _map_subschemas(kept, lambda sub: _normalize(sub, defs, resolving))


def transform_schema(
    schema: Mapping[str, Any], transform: Callable[[Dict[str, Any]], Dict[str, Any]]
) -> Dict[str, Any]:
    """Apply `transform` to every subschema of a normalized schema, innermost first."""
    return transform(
        _map_subschemas(schema, lambda sub: transform_schema(sub, transform))
    )


def _map_subschemas(
    node: Mapping[str, Any], fn: Callable[[Mapping[str, Any]], Dict[str, Any]]
) -> Dict[str, Any]:
    def apply(value: Any) -> Any:
        return fn(value) if isinstance(value, Mapping) else copy.deepcopy(value)

    result: Dict[str, Any] = {}
    for key, value in node.items():
        if key in _SCHEMA_MAP_KEYS and isinstance(value, Mapping):
            result[key] = {name: apply(sub) for name, sub in value.items()}
        elif key in _SCHEMA_KEYS:
            result[key] = apply(value)
        elif key in _SCHEMA_LIST_KEYS and isinstance(value, list):
            result[key] = [apply(sub) for sub in value]
        else:
            result[key] = copy.deepcopy(value)
    return result


def _inline_ref(
    node: Mapping[str, Any],
    defs: Mapping[str, Mapping[str, Any]],
    resolving: FrozenSet[str],
) -> Tuple[Dict[str, Any], FrozenSet[str]]:
    ref = node["$ref"]
    target = _resolve_definition(ref, defs)
    if ref in resolving:
        raise RecursiveSchemaError(f"recursive schema reference: {ref}")
    merged = dict(target)
    merged.update((key, value) for key, value in node.items() if key != "$ref")
    return merged, resolving | {ref}


def _resolve_definition(
    ref: Any, defs: Mapping[str, Mapping[str, Any]]
) -> Mapping[str, Any]:
    """Resolve `#/$defs/<name>` or `#/definitions/<name>` against its own table.

    Anything deeper or non-local is refused rather than guessed: resolving by
    the last path segment alone would silently substitute an unrelated schema.
    """
    parts = ref.split("/") if isinstance(ref, str) else []
    if len(parts) == 3 and parts[0] == "#" and parts[1] in _DEFINITION_TABLES:
        name = parts[2].replace("~1", "/").replace("~0", "~")
        target = defs[parts[1]].get(name)
        if isinstance(target, Mapping):
            return target
    raise ValueError(f"unresolved schema reference: {ref}")


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
