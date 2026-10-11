"""Asset/target validation, durable identity, and engagement-tree sharing."""

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from kerkov.core.knowledge_base.engagement_kb import EngagementKnowledgeBase
from kerkov.core.knowledge_base.findings_ledger import FindingsLedger, identity_error
from kerkov.core.knowledge_base.models import LedgerSection, Resource
from kerkov.tools.ledger_tool import LedgerTool


@pytest.mark.parametrize(
    "kind,value,expected",
    [
        ("domain", " BÜCHER.Example. ", "xn--bcher-kva.example"),
        ("ip", "2001:0DB8:0:0::1", "2001:db8::1"),
        (
            "url",
            "HTTPS://Example.COM/Path?Key=Value#Section",
            "https://example.com/Path?Key=Value#Section",
        ),
        ("url", "http://[2001:0DB8::1]:8080/Path", "http://[2001:db8::1]:8080/Path"),
        ("repository", "~/repo/../API", str(Path.home() / "API")),
    ],
)
def test_resource_normalizes_only_identity_components(kind, value, expected):
    assert Resource(kind=kind, value=value).value == expected


def test_repository_resolves_symlinks_before_parent_traversal(tmp_path):
    nested = tmp_path / "actual" / "nested"
    nested.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(nested, target_is_directory=True)
    resource = Resource(kind="repository", value=str(alias / ".."))
    assert resource.value == str(nested.parent.resolve())


@pytest.mark.parametrize(
    "entry",
    [
        {},
        {"kind": "host", "value": "example.com"},
        {"kind": "domain", "value": "bad_name.example"},
        {"kind": "domain", "value": "-bad.example"},
        {"kind": "domain", "value": "example..com"},
        {"kind": "domain", "value": "127.0.0.1"},
        {"kind": "domain", "value": "a" * 64 + ".com"},
        {"kind": "ip", "value": "999.1.1.1"},
        {"kind": "ip", "value": "10.0.0.0/24"},
        {"kind": "ip", "value": "fe80::1%eth0"},
        {"kind": "url", "value": "example.com/path"},
        {"kind": "url", "value": "ftp://example.com"},
        {"kind": "url", "value": "https://"},
        {"kind": "url", "value": "https://example.com:99999"},
        {"kind": "url", "value": "https://example.com:"},
        {"kind": "url", "value": "https://user:secret@example.com"},
        {"kind": "url", "value": "https://example.com/a b"},
        {"kind": "url", "value": "https://example.com/\npath"},
        {"kind": "repository", "value": "relative/path"},
        {"kind": "repository", "value": "https://github.com/org/repo"},
        {"kind": "repository", "value": "git@github.com:org/repo.git"},
        {"kind": "domain", "value": " "},
        {"kind": "domain", "value": 123},
        {"kind": "domain", "value": "example.com", "name": " "},
        {"kind": "domain", "value": "example.com", "note": "unexpected"},
    ],
)
def test_invalid_resource_is_rejected_by_model_and_tool_validation(entry):
    with pytest.raises(ValidationError):
        Resource.model_validate(entry)
    assert identity_error(LedgerSection.ASSETS, entry)


@pytest.fixture
def ledger(tmp_path):
    kb = EngagementKnowledgeBase("root", tmp_path)
    yield FindingsLedger(kb)
    kb.close()


def add(ledger, section, kind, value, **extra):
    return asyncio.run(
        ledger.add(
            section, {"kind": kind, "value": value, **extra}, engagement_id="agent"
        )
    )


def test_direct_ledger_writes_validate_before_persisting(ledger):
    with pytest.raises(ValidationError):
        add(ledger, LedgerSection.ASSETS, "ip", "invalid")
    assert asyncio.run(ledger.list()) == []


def test_identity_dedup_separates_roles_kinds_and_case_sensitive_values(ledger):
    assets, targets = LedgerSection.ASSETS, LedgerSection.TARGETS
    first = add(ledger, assets, "domain", "Example.COM", name="API")
    assert add(ledger, assets, "domain", "example.com.") == first
    assert add(ledger, targets, "domain", "example.com") != first
    assert add(ledger, assets, "url", "https://example.com") != first
    upper = add(ledger, targets, "url", "HTTPS://Example.COM/Path?q=A")
    assert add(ledger, targets, "url", "https://example.com/Path?q=A") == upper
    assert add(ledger, targets, "url", "https://example.com/path?q=A") != upper
    assert add(ledger, targets, "url", "https://example.com/Path?q=a") != upper
    assert add(ledger, assets, "repository", "/work/API") != add(
        ledger, assets, "repository", "/work/api"
    )
    assert (
        next(e for e in asyncio.run(ledger.list()) if e.id == first).entry["name"]
        == "API"
    )


def test_inventory_persists_and_is_shared_but_not_owned_by_agent(ledger, tmp_path):
    add(ledger, LedgerSection.ASSETS, "repository", "/work/API", name="Production API")
    add(ledger, LedgerSection.TARGETS, "ip", "2001:0DB8::1")
    assert ledger._kb.delete_engagement_ledger_rows("agent") == 0
    peer_kb = EngagementKnowledgeBase("root", tmp_path)
    try:
        peer = FindingsLedger(peer_kb)
        rows = asyncio.run(peer.list())
        assert len(rows) == 2
        assert {e.engagement_id for e in rows} == {"root"}
        block = asyncio.run(peer.serialize())
        assert "ASSETS\n  - repository /work/API — Production API" in block
        assert "TARGETS\n  - ip 2001:db8::1" in block
        add(ledger, LedgerSection.ASSETS, "domain", "example.com")
        assert "example.com" in asyncio.run(peer.serialize())
    finally:
        peer_kb.close()
    ledger._kb.close()
    assert len(asyncio.run(ledger.list())) == 3


def test_section_scoped_remove_preserves_other_sections(ledger):
    target_id = add(ledger, LedgerSection.TARGETS, "domain", "example.com")
    assert not asyncio.run(ledger.remove(target_id, section=LedgerSection.ASSETS))
    assert len(asyncio.run(ledger.list())) == 1
    assert asyncio.run(ledger.remove(target_id, section=LedgerSection.TARGETS))


def test_inventory_survives_context_budget_before_host_enumeration(ledger):
    add(ledger, LedgerSection.ASSETS, "domain", "protected.example")
    add(ledger, LedgerSection.TARGETS, "domain", "target.example")
    for i in range(30):
        asyncio.run(
            ledger.add(
                LedgerSection.HOSTS, {"ip": f"10.0.0.{i}"}, engagement_id="agent"
            )
        )
    block = asyncio.run(FindingsLedger(ledger._kb, max_injected_bytes=300).serialize())
    assert "protected.example" in block and "target.example" in block
    assert "omitted" in block
    assert len(asyncio.run(ledger.list())) == 32


def test_tool_adds_resources_without_changing_existing_observations(ledger):
    tool = LedgerTool(lambda: ledger, lambda: "agent")
    for section, entry in [
        ("assets", {"kind": "domain", "value": "Example.COM"}),
        ("targets", {"kind": "ip", "value": "10.0.0.5"}),
        ("hosts", {"hostname": "Example.COM", "os": "Linux"}),
        ("services", {"host": "10.0.0.5", "port": 443}),
    ]:
        assert asyncio.run(
            tool.execute({"op": "add", "section": section, "entry": entry})
        ).success
    assert len(asyncio.run(ledger.list())) == 4
    hosts = asyncio.run(ledger.list(LedgerSection.HOSTS))
    assert hosts[0].engagement_id == "agent"
    assert hosts[0].entry == {"hostname": "Example.COM", "os": "Linux"}


def test_resource_context_is_normalized_without_changing_identity(ledger):
    entry_id = add(
        ledger,
        LedgerSection.TARGETS,
        "domain",
        "Example.COM",
        environment=" Staging ",
        tags=["api", " api ", "internal"],
        notes=" VPN required\nContact ops ",
    )
    row = asyncio.run(ledger.list())[0]
    assert row.id == entry_id
    assert row.entry["environment"] == "Staging"
    assert row.entry["tags"] == ["api", "internal"]
    assert row.entry["notes"] == "VPN required\nContact ops"
    assert "VPN required" in asyncio.run(ledger.serialize())
    assert add(ledger, LedgerSection.TARGETS, "domain", "example.com") == entry_id


@pytest.mark.parametrize(
    "details",
    [
        {"environment": "x" * 129},
        {"notes": "x" * 4097},
        {"environment": "bad\nvalue"},
        {"notes": "bad\x00value"},
        {"tags": ["x" * 65]},
        {"tags": ["tag"] * 21},
        {"tags": "api"},
        {"tags": ["bad,tag"]},
        {"tags": [""]},
    ],
)
def test_resource_rejects_invalid_context(details):
    with pytest.raises(ValidationError):
        Resource(kind="domain", value="example.com", **details)
