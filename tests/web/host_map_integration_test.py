"""Render the stored reachability contract through the real host map."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Page, expect

from kerkov.core.knowledge_base.engagement_kb import EngagementKnowledgeBase
from kerkov.core.knowledge_base.findings_ledger import FindingsLedger
from kerkov.core.knowledge_base.models import LedgerSection
from kerkov.tools.container_lifecycle_tool import ContainerLifecycleTool
from tests.tools.container_lifecycle_tool_test import _compose_record, _FakeDockerRunner
from tests.web.inventory_panel_browser_test import (
    browser,
    dashboard,
    emit,
    last_request,
    sent,
)
from tests.web.network_map_browser_test import load_network


def run_async(operation):
    # Playwright's sync API already runs an event loop on the test thread.
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, operation).result()


def test_stored_routes_render_as_one_host_and_one_service_bundle(
    dashboard: Page, tmp_path: Path
) -> None:
    kb = EngagementKnowledgeBase("host-map", tmp_path)
    ledger = FindingsLedger(kb)

    async def build_snapshot() -> dict:
        async def resource(kind: str, value: str) -> int:
            return await ledger.add(
                LedgerSection.TARGETS,
                {"kind": kind, "value": value},
                engagement_id="host-map",
            )

        domain = await resource("domain", "api.example.com")
        address = await resource("ip", "192.0.2.14")
        await resource("url", "https://api.example.com/v1")
        evidence = await kb.add_artifact(
            engagement_id="host-map", body="private tool output stays in the KB"
        )
        await ledger.add(
            LedgerSection.CONNECTIONS,
            {
                "source_id": domain,
                "target_id": address,
                "relation": "resolves_to",
                "provenance": "observed",
            },
            engagement_id="host-map",
            artifact_ref=evidence,
        )
        for protocol, service in (("tcp", "HTTPS"), ("udp", "QUIC")):
            await ledger.add(
                LedgerSection.CONNECTIONS,
                {
                    "source_vantage": "assessment-container",
                    "target_id": domain,
                    "relation": "reaches",
                    "protocol": protocol,
                    "port": 443,
                    "service": service,
                    "observed_at": "2026-10-05T12:30:00-04:00",
                    "provenance": "observed",
                },
                engagement_id="host-map",
                artifact_ref=evidence,
            )
        return {
            "request_id": "",
            "context_id": "real-ledger",
            "engagement_id": "host-map",
            "available": True,
            "entries": [
                row.model_dump(mode="json")
                for row in await ledger.list(
                    sections=(LedgerSection.ASSETS, LedgerSection.TARGETS)
                )
            ],
            "graph": await ledger.graph(),
        }

    try:
        payload = run_async(build_snapshot())
        emit(dashboard, "inventory.snapshot", payload)
        dashboard.get_by_role("tab", name="Network map").click()
        expect(dashboard.locator("#network-node-count")).to_have_text("1")
        expect(dashboard.locator(".network-node")).to_have_count(2)
        expect(dashboard.locator(".network-edge")).to_have_count(1)
        dashboard.locator(".network-edge").focus()
        dashboard.keyboard.press("Enter")
        details = dashboard.locator("#network-details")
        expect(details).to_contain_text("HTTPS :443")
        expect(details).to_contain_text("QUIC :443")
        expect(details).to_contain_text("TCP")
        expect(details).to_contain_text("UDP")
        expect(details).to_contain_text("2026-10-05T16:30:00+00:00")
        expect(details).not_to_contain_text("private tool output")

        # Target filtering keeps the real observation origin as route context.
        dashboard.locator("#network-role").select_option("target")
        expect(dashboard.locator(".network-edge")).to_have_count(1)
        dashboard.locator('[data-node-id="host:api.example.com"]').focus()
        dashboard.keyboard.press("Enter")
        expect(details).to_contain_text("https://api.example.com/v1")
        expect(details).to_contain_text("192.0.2.14")

        # A fresh snapshot withdraws a deleted assertion without losing the host.
        for row in run_async(ledger.list(LedgerSection.CONNECTIONS)):
            if row.entry["relation"] == "reaches":
                run_async(ledger.remove(row.id))
        payload["graph"] = run_async(ledger.graph())
        emit(dashboard, "inventory.snapshot", payload)
        expect(dashboard.locator(".network-edge")).to_have_count(0)
        expect(dashboard.locator(".network-node")).to_have_count(1)
        expect(details).to_contain_text("No matching route")
    finally:
        kb.close()


@pytest.mark.parametrize("width", [1440, 390])
def test_two_container_projects_and_htb_vpn_routes_remain_independent(
    dashboard: Page, tmp_path: Path, width: int
) -> None:
    """Exercise the combined release workflow with fake Docker and real ledger data."""
    dashboard.set_viewport_size({"width": width, "height": 1000})
    kb = EngagementKnowledgeBase("mixed-labs", tmp_path)
    ledger = FindingsLedger(kb)

    async def operate(project: str, port: int, action: str = "start_wait") -> None:
        directory = tmp_path / project
        directory.mkdir(exist_ok=True)
        (directory / "compose.yaml").write_text(
            "services:\n  web:\n    image: example/web\n"
        )
        record = _compose_record(health="healthy", published_port=port)
        record["Name"] = f"{project}-web-1"
        tool = ContainerLifecycleTool(
            runner=_FakeDockerRunner(snapshots=[json.dumps([record])]),
            execution_enabled=True,
            ledger_getter=lambda: ledger,
        )
        with patch(
            "kerkov.tools.container_lifecycle_tool.get_current_workspace",
            return_value=tmp_path,
        ):
            result = await tool.execute(
                {
                    "action": action,
                    "compose_dir": str(directory),
                    "project_name": project,
                }
            )
        assert result.success, result.error

    async def snapshot() -> dict:
        return {
            "request_id": "",
            "context_id": "context-1",
            "engagement_id": "engagement-1",
            "available": True,
            "entries": [
                row.model_dump(mode="json")
                for row in await ledger.list(LedgerSection.TARGETS)
            ],
            "graph": await ledger.graph(),
        }

    async def prepare() -> None:
        await operate("lab-a", 18080)
        await operate("lab-b", 18081)
        vpn = await ledger.add(
            LedgerSection.ASSETS,
            {"kind": "ip", "value": "10.10.14.5", "name": "HTB VPN"},
            engagement_id="mixed-labs",
        )
        target = await ledger.add(
            LedgerSection.TARGETS,
            {"kind": "ip", "value": "10.10.10.3", "name": "HTB target"},
            engagement_id="mixed-labs",
        )
        await ledger.add_connection(
            {
                "source_id": vpn,
                "target_id": target,
                "relation": "reaches",
                "protocol": "tcp",
                "port": 22,
                "service": "SSH",
                "provenance": "declared",
                "note": "Test fixture: declared HTB VPN route, not live reachability.",
            }
        )

    try:
        run_async(prepare())
        payload = run_async(snapshot())
        load_network(dashboard, payload["entries"], payload["graph"])
        expect(dashboard.locator(".network-node.kind-container")).to_have_count(2)
        expect(dashboard.locator(".network-edge")).to_have_count(3)
        for project, port in (("lab-a", 18080), ("lab-b", 18081)):
            card = dashboard.locator(".network-node").filter(
                has_text=f"{project}-web-1"
            )
            card.click()
            expect(dashboard.locator("#network-details")).to_contain_text(
                f"{port} → 80/tcp"
            )
        vpn_route = dashboard.locator(".network-edge.provenance-declared")
        vpn_route.focus()
        dashboard.keyboard.press("Enter")
        expect(dashboard.locator("#network-details")).to_contain_text("SSH :22")
        expect(dashboard.locator("#network-details")).to_contain_text("10.10.14.5")

        # Stopping one Compose project must preserve the other project and VPN.
        run_async(operate("lab-a", 18080, "down"))
        emit(dashboard, "inventory.snapshot", run_async(snapshot()))
        expect(dashboard.locator(".network-edge")).to_have_count(2)
        expect(dashboard.locator(".network-edge.provenance-declared")).to_have_count(1)
        expect(
            dashboard.locator(".network-node").filter(has_text="lab-a-web-1")
        ).to_contain_text("stopped")
        expect(
            dashboard.locator(".network-node").filter(has_text="lab-b-web-1")
        ).to_contain_text("running")
    finally:
        kb.close()


def test_container_start_stop_restart_automatically_refresh_map_connections(
    dashboard: Page, tmp_path: Path
) -> None:
    compose_dir = tmp_path / "lab"
    compose_dir.mkdir()
    (compose_dir / "compose.yaml").write_text(
        "services:\n  web:\n    image: example/web\n"
    )
    kb = EngagementKnowledgeBase("container-map", tmp_path)
    ledger = FindingsLedger(kb)
    tool = ContainerLifecycleTool(
        runner=_FakeDockerRunner(
            snapshots=[json.dumps([_compose_record(health="healthy")])]
        ),
        execution_enabled=True,
        ledger_getter=lambda: ledger,
    )

    async def operate(action: str):
        with patch(
            "kerkov.tools.container_lifecycle_tool.get_current_workspace",
            return_value=tmp_path,
        ):
            result = await tool.execute(
                {"action": action, "compose_dir": str(compose_dir)}
            )
        assert result.success
        assert json.loads(result.output)["inventory"]["status"] == "registered"
        assert await ledger.list(LedgerSection.CONNECTIONS) == []
        return {
            "context_id": "container-map",
            "engagement_id": "container-map",
            "available": True,
            "entries": [
                row.model_dump(mode="json")
                for row in await ledger.list(LedgerSection.TARGETS)
            ],
            "graph": await ledger.graph(),
        }

    try:
        request = last_request(dashboard, "inventory.list")
        emit(
            dashboard,
            "inventory.snapshot",
            {
                "request_id": request["request_id"],
                "context_id": "container-map",
                "available": True,
                "entries": [],
                "graph": {"nodes": [], "edges": []},
            },
        )
        dashboard.get_by_role("tab", name="Network map").click()
        # Settle the tab's initial refresh before exercising tool-driven refreshes.
        dashboard.wait_for_function(
            "() => window.testSocket.sent.filter(m => m.type === 'inventory.list').length >= 2"
        )
        request = last_request(dashboard, "inventory.list")
        emit(
            dashboard,
            "inventory.snapshot",
            {
                "request_id": request["request_id"],
                "context_id": "container-map",
                "available": True,
                "entries": [],
                "graph": {"nodes": [], "edges": []},
            },
        )
        for action in ("start_wait", "down", "start_wait"):
            payload = run_async(operate(action))
            count = len(sent(dashboard, "inventory.list"))
            emit(
                dashboard,
                "tool.result",
                {"name": "container_lifecycle", "success": True},
            )
            dashboard.wait_for_function(
                "count => window.testSocket.sent.filter(m => m.type === 'inventory.list').length > count",
                arg=count,
            )
            request = last_request(dashboard, "inventory.list")
            emit(
                dashboard,
                "inventory.snapshot",
                {**payload, "request_id": request["request_id"]},
            )
            expect(dashboard.locator(".network-node.kind-container")).to_have_count(1)
            expect(dashboard.locator(".network-edge")).to_have_count(
                0 if action == "down" else 1
            )
            if action == "start_wait":
                edge = dashboard.locator(".network-edge.provenance-published")
                expect(edge).to_contain_text("TCP :8080 → :80")
                edge.focus()
                dashboard.keyboard.press("Enter")
                expect(dashboard.locator("#network-details")).to_contain_text(
                    "Port mappings do not establish reachability"
                )
        assert sent(dashboard, "user.message") == []
    finally:
        kb.close()
