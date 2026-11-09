"""Mapped MCP tool names fit every provider and never collide."""

import re

from kerkov.core.mcp.naming import MAX_TOOL_NAME_LENGTH, map_tool_names

_PROVIDER_SAFE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")


def test_plain_names_are_prefixed_with_the_server_key() -> None:
    assert map_tool_names("burp", ["scan", "report"], set()) == {
        "mcp__burp__report": "report",
        "mcp__burp__scan": "scan",
    }


def test_unsafe_and_long_names_are_sanitized_bounded_and_reversible() -> None:
    originals = ["get.issue", "a" * 120, "space name", "get_issue"]

    mapped = map_tool_names("tracker", originals, set())

    assert sorted(mapped.values()) == sorted(originals)
    for name in mapped:
        assert len(name) <= MAX_TOOL_NAME_LENGTH
        assert _PROVIDER_SAFE.fullmatch(name)
    assert mapped["mcp__tracker__get_issue"] == "get_issue"


def test_names_that_sanitize_alike_stay_distinct() -> None:
    mapped = map_tool_names("srv", ["a.b", "a-b", "a b", "a_b"], set())

    assert len(mapped) == 4


def test_taken_names_are_avoided() -> None:
    mapped = map_tool_names("srv", ["exec"], {"mcp__srv__exec"})

    (name,) = mapped
    assert name != "mcp__srv__exec"
    assert mapped[name] == "exec"


def test_mapping_is_deterministic_regardless_of_input_order() -> None:
    first = map_tool_names("srv", ["b", "a.x", "c" * 90], set())
    second = map_tool_names("srv", ["c" * 90, "a.x", "b"], set())

    assert first == second
