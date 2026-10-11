"""Docker observations persist independently of endpoint identity and user context."""

import asyncio

from kerkov.core.knowledge_base.containers import published_ports, register_containers
from kerkov.core.knowledge_base.engagement_kb import EngagementKnowledgeBase
from kerkov.core.knowledge_base.findings_ledger import FindingsLedger
from kerkov.core.knowledge_base.models import LedgerSection


def payload(*names, state="running", ready=True):
    return {
        "compose_dir": "/labs/demo",
        "status": "ready",
        "ready": ready,
        "readiness_basis": "HTTP probe ready",
        "probe_url": "http://127.0.0.1:8000/",
        "log_artifact": "/artifacts/start.log",
        "containers": [
            {
                "name": name,
                "service": name,
                "state": state,
                "health": "healthy",
                "published_ports": [
                    {
                        "host": "0.0.0.0",
                        "host_port": 8000 + index,
                        "container_port": 80,
                        "protocol": "tcp",
                    }
                ],
            }
            for index, name in enumerate(names)
        ],
    }


def test_registration_is_shared_deduplicated_and_preserves_manual_context(tmp_path):
    async def scenario():
        kb = EngagementKnowledgeBase("root", tmp_path)
        ledger = FindingsLedger(kb)
        try:
            manual = await ledger.add(
                LedgerSection.TARGETS,
                {
                    "kind": "url",
                    "value": "http://127.0.0.1:8000/",
                    "name": "My lab",
                    "environment": "Staging",
                    "tags": ["manual"],
                    "notes": "Keep me",
                },
                engagement_id="root",
            )
            data = payload("flask", "activemq", "neo4j")
            first = await register_containers(ledger, data, project_name="labs")
            assert first == await register_containers(ledger, data, project_name="labs")
            rows = await ledger.list(LedgerSection.CONTAINERS)
            assert len(rows) == 3
            assert {row.engagement_id for row in rows} == {"root"}
            assert all(row.entry["ready"] for row in rows)
            assert manual in rows[0].entry["target_ids"]
            await register_containers(
                ledger, {**data, "probe_url": ""}, project_name="labs"
            )
            assert (
                manual
                in (await ledger.list(LedgerSection.CONTAINERS))[0].entry["target_ids"]
            )
            assert len(await ledger.list(LedgerSection.TARGETS)) == 2
            saved = next(
                row
                for row in await ledger.list(LedgerSection.TARGETS)
                if row.id == manual
            )
            assert saved.entry["name"] == "My lab" and saved.entry["notes"] == "Keep me"
            assert await ledger.list(LedgerSection.CONNECTIONS) == []
            assert "CONTAINERS" in await ledger.serialize()
            assert kb.delete_engagement_ledger_rows("child") == 0
            peer = EngagementKnowledgeBase("root", tmp_path)
            try:
                assert (
                    len(await FindingsLedger(peer).list(LedgerSection.CONTAINERS)) == 3
                )
            finally:
                peer.close()
        finally:
            kb.close()

    asyncio.run(scenario())


def test_stop_status_removal_and_port_clearing_are_project_scoped(tmp_path):
    async def scenario():
        kb = EngagementKnowledgeBase("root", tmp_path)
        ledger = FindingsLedger(kb)
        try:
            await register_containers(ledger, payload("web", "db"), project_name="a")
            await register_containers(ledger, payload("web"), project_name="b")
            stopped = {**payload(), "status": "stopped", "ready": False}
            await register_containers(ledger, stopped, project_name="a")
            rows = await ledger.list(LedgerSection.CONTAINERS)
            assert all(
                row.entry["state"] == "stopped" and not row.entry["ready"]
                for row in rows
                if row.entry["project_name"] == "a"
            )
            assert (
                next(row for row in rows if row.entry["project_name"] == "b").entry[
                    "state"
                ]
                == "running"
            )
            data = payload("web", ready=False)
            data["containers"][0]["published_ports"] = []
            data["containers"][0]["health"] = ""
            await register_containers(ledger, data, project_name="a")
            rows = await ledger.list(LedgerSection.CONTAINERS)
            web = next(
                row
                for row in rows
                if row.entry["project_name"] == "a" and row.entry["name"] == "web"
            )
            assert (
                web.entry["ports"] == []
                and web.entry["target_ids"] == []
                and web.entry["health"] == ""
            )
            assert web.entry["ready"] is False
            assert (
                next(row for row in rows if row.entry["name"] == "db").entry["state"]
                == "removed"
            )
            assert await ledger.list(
                LedgerSection.TARGETS
            )  # Keep saved targets/history.
            data["containers_truncated"] = True
            await register_containers(ledger, data, project_name="b")
            await register_containers(
                ledger, {**payload(), "containers_truncated": True}, project_name="b"
            )
            assert (
                next(
                    row
                    for row in await ledger.list(LedgerSection.CONTAINERS)
                    if row.entry["project_name"] == "b"
                ).entry["state"]
                == "running"
            )
        finally:
            kb.close()

    asyncio.run(scenario())


def test_structured_ports_reject_invalid_and_internal_only_mappings():
    records = [
        {"URL": "::", "PublishedPort": 7474, "TargetPort": 7474, "Protocol": "tcp"},
        {"TargetPort": 80},
        {"PublishedPort": True, "TargetPort": 80},
        {"PublishedPort": 70000, "TargetPort": 80},
        {"URL": "bad", "PublishedPort": 80, "TargetPort": 80},
        {"PublishedPort": 53, "TargetPort": 53, "Protocol": "udp"},
    ]
    ports = published_ports(records)
    assert len(ports) == 2
    assert ports[0]["host"] == "::" and ports[1]["protocol"] == "udp"
