"""Recorded SSH access is endpoint-specific and never projects raw secrets."""

from kerkov.core.knowledge_base.models import LedgerEntry
from kerkov.core.knowledge_base.ssh_access import project_ssh_access


def _create_mock_rows(**overrides):
    return [
        LedgerEntry(
            id=1,
            engagement_id="root",
            section="assets",
            entry={"kind": "domain", "value": "example.com"},
        ),
        LedgerEntry(
            id=2,
            engagement_id="agent",
            section="credentials",
            entry={
                "kind": "ssh",
                "host": "EXAMPLE.COM.",
                "username": "audit",
                "port": 2222,
                "private_key_file": "/keys/audit",
                "known_hosts_file": "/keys/trusted",
                "secret": "PRIVATE-KEY-CONTENTS",
                "password": "PASSWORD",
                "note": "PRIVATE-NOTE",
                **overrides,
            },
        ),
    ]


def test_exact_host_access_excludes_secret_payloads():
    result = project_ssh_access(_create_mock_rows(), 1)
    assert result == {
        "resource": {"kind": "domain", "value": "example.com"},
        "candidates": [
            {
                "id": 2,
                "complete": True,
                "fields": {
                    "user": "audit",
                    "port": 2222,
                    "private_key_file": "/keys/audit",
                    "known_hosts_file": "/keys/trusted",
                },
            }
        ],
        "truncated": False,
    }
    assert "PRIVATE" not in str(result) and "PASSWORD" not in str(result)


def test_no_matching_from_source_names_relationships_or_other_hosts():
    for overrides in (
        {"host": "other.example.com"},
        {"host": None, "source": "example.com"},
        {"host": "example.com.evil"},
        {"kind": "password"},
        {"private_key_file": "-----BEGIN PRIVATE KEY-----"},
        {"port": True},
    ):
        assert project_ssh_access(_create_mock_rows(**overrides), 1)["candidates"] == []


def test_partial_credentials_do_not_invent_ports_or_trusted_hosts():
    result = project_ssh_access(_create_mock_rows(port=None, known_hosts_file=None), 1)
    candidate = result["candidates"][0]
    assert candidate["complete"] is False
    assert (
        "port" not in candidate["fields"]
        and "known_hosts_file" not in candidate["fields"]
    )


def test_integral_provider_numbers_and_ssh_key_aliases_are_supported():
    result = project_ssh_access(_create_mock_rows(port=2222.0, kind="ssh-key"), 1)
    assert result["candidates"][0]["fields"]["port"] == 2222


def test_missing_resource_is_not_a_credential_query():
    assert project_ssh_access(_create_mock_rows(), 2) is None
