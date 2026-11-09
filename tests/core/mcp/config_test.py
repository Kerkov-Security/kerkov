"""mcp.json parsing, placeholder resolution and round-tripping edits."""

import json
from pathlib import Path

import pytest

from kerkov.core.mcp import config as mcp_config
from kerkov.core.mcp.config import (
    McpConfigError,
    MissingKeyError,
    add_server,
    load_mcp_config,
    parse_server,
    remove_server,
    resolve_config_path,
    resolve_server,
)


def _lookup(values):
    def lookup(name):
        if name in values:
            return values[name], "keyring"
        return None, ""

    return lookup


def _write(path: Path, document) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_infers_stdio_from_command_and_reads_claude_code_shape(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "mcp.json",
        {
            "mcpServers": {
                "burp": {
                    "command": "npx",
                    "args": ["-y", "@example/burp-mcp"],
                    "env": {"BURP_API_KEY": "${BURP_API_KEY}"},
                }
            }
        },
    )

    config = load_mcp_config(path)

    (server,) = config.servers
    assert server.transport == "stdio"
    assert server.args == ("-y", "@example/burp-mcp")
    assert server.placeholders() == ["BURP_API_KEY"]
    assert config.warnings == ()


def test_missing_file_is_an_empty_inventory(tmp_path: Path) -> None:
    assert load_mcp_config(tmp_path / "absent.json").servers == ()


def test_broken_file_raises(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(McpConfigError):
        load_mcp_config(path)


def test_bad_entry_is_reported_without_hiding_the_others(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "mcp.json",
        {"mcpServers": {"good": {"command": "x"}, "bad": {"type": "stdio"}}},
    )

    config = load_mcp_config(path)

    assert [s.key for s in config.servers] == ["good"]
    assert "command" in config.errors["bad"]


@pytest.mark.parametrize("key", ["a__b", "_lead", "has space", "x" * 33, ""])
def test_server_keys_that_would_break_tool_names_are_rejected(key: str) -> None:
    with pytest.raises(McpConfigError):
        parse_server(key, {"command": "x"})


def test_unknown_keys_warn_and_literal_secrets_warn() -> None:
    _, warnings = parse_server(
        "intel",
        {
            "type": "http",
            "url": "https://mcp.example.com",
            "headers": {"Authorization": "Bearer abc123"},
            "oauth": {"clientId": "x"},
        },
    )

    assert any("unknown key 'oauth'" in w for w in warnings)
    assert any("literal secret" in w for w in warnings)


def test_placeholders_resolve_with_defaults_and_report_sources() -> None:
    server, _ = parse_server(
        "srv",
        {
            "command": "${TOOL_BIN:-tool}",
            "args": ["--region", "${REGION:-eu}"],
            "env": {"API_KEY": "${API_KEY}"},
        },
    )

    resolved = resolve_server(server, _lookup({"API_KEY": "s3cret"}))

    assert resolved.command == "tool"
    assert resolved.args == ("--region", "eu")
    assert resolved.env == {"API_KEY": "s3cret"}
    assert resolved.key_sources == {"API_KEY": "keyring"}
    assert "s3cret" not in repr(resolved)


def test_missing_required_key_names_every_missing_placeholder() -> None:
    server, _ = parse_server(
        "srv", {"command": "x", "env": {"A": "${KEY_A}", "B": "${KEY_B}"}}
    )

    with pytest.raises(MissingKeyError) as raised:
        resolve_server(server, _lookup({}))

    assert raised.value.names == ["KEY_A", "KEY_B"]


def test_absent_optional_key_drops_the_entry_instead_of_sending_it_empty() -> None:
    server, _ = parse_server(
        "srv",
        {
            "command": "x",
            "args": ["--token", "${TOKEN}", "--verbose"],
            "env": {"TOKEN": "${TOKEN}", "MODE": "fast"},
            "optional": ["TOKEN"],
        },
    )

    resolved = resolve_server(server, _lookup({}))

    assert resolved.env == {"MODE": "fast"}
    assert resolved.args == ("--token", "--verbose")
    assert resolved.absent_optional == ("TOKEN",)


def test_optional_does_not_apply_to_the_command() -> None:
    server, _ = parse_server("srv", {"command": "${BIN}", "optional": ["BIN"]})

    with pytest.raises(MissingKeyError):
        resolve_server(server, _lookup({}))


def test_kerkov_provider_keys_never_reach_a_server() -> None:
    server, _ = parse_server(
        "srv",
        {"command": "x", "env": {"ANTHROPIC_API_KEY": "${ANTHROPIC_API_KEY}"}},
    )

    resolved = resolve_server(server, _lookup({"ANTHROPIC_API_KEY": "sk-ant"}))

    assert resolved.env == {}


def test_tool_filter_applies_deny_over_allow() -> None:
    server, _ = parse_server(
        "srv",
        {
            "command": "x",
            "tools": {"allow": ["scan_*", "report"], "deny": ["scan_full"]},
        },
    )

    assert server.tools.permits("scan_quick")
    assert server.tools.permits("report")
    assert not server.tools.permits("scan_full")
    assert not server.tools.permits("delete_all")


def test_add_and_remove_preserve_unknown_keys_and_other_servers(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "mcp.json",
        {
            "mcpServers": {"keep": {"command": "x", "customKey": [1, 2]}},
            "topLevel": True,
        },
    )

    add_server(path, "new", {"type": "stdio", "command": "y"})
    remove_server(path, "new")

    document = json.loads(path.read_text(encoding="utf-8"))
    assert document == {
        "mcpServers": {"keep": {"command": "x", "customKey": [1, 2]}},
        "topLevel": True,
    }


def test_add_refuses_a_duplicate_and_writes_no_resolved_secret(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "mcp.json"
    add_server(path, "srv", {"command": "x", "env": {"K": "${K}"}})

    with pytest.raises(McpConfigError):
        add_server(path, "srv", {"command": "y"})
    assert json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["srv"]["env"] == {
        "K": "${K}"
    }


def test_config_is_never_discovered_in_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (".mcp.json", "mcp.json"):
        _write(tmp_path / name, {"mcpServers": {"planted": {"command": "evil"}}})
    (tmp_path / "config").mkdir()
    _write(
        tmp_path / "config" / "mcp.json",
        {"mcpServers": {"planted": {"command": "evil"}}},
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mcp_config, "USER_CONFIG_PATH", tmp_path / "home" / "mcp.json")
    monkeypatch.setattr(
        mcp_config, "bundled_asset_path", lambda name: tmp_path / "checkout" / name
    )

    assert resolve_config_path() == tmp_path / "home" / "mcp.json"


def test_explicit_path_wins(tmp_path: Path) -> None:
    assert resolve_config_path(str(tmp_path / "x.json")) == tmp_path / "x.json"


@pytest.mark.parametrize(
    "url,warned",
    [
        ("http://mcp.example.com/mcp", True),
        ("https://mcp.example.com/mcp", False),
        ("http://127.0.0.1:9000/mcp", False),
        ("http://localhost/mcp", False),
    ],
)
def test_headers_over_plain_remote_http_warn(url: str, warned: bool) -> None:
    _, warnings = parse_server(
        "intel", {"url": url, "headers": {"Authorization": "Bearer ${K}"}}
    )

    assert any("plain HTTP" in w for w in warnings) is warned


def test_http_header_placeholders_resolve() -> None:
    server, _ = parse_server(
        "intel",
        {
            "url": "https://${HOST:-mcp.example.com}/mcp",
            "headers": {"Authorization": "Bearer ${K}"},
        },
    )

    resolved = resolve_server(server, _lookup({"K": "abc"}))

    assert resolved.url == "https://mcp.example.com/mcp"
    assert resolved.headers == {"Authorization": "Bearer abc"}
