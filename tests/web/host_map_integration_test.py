"""Render the stored reachability contract through the real host map."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Page, expect

from kerkov.core.knowledge_base.engagement_kb import EngagementKnowledgeBase
from kerkov.core.knowledge_base.findings_ledger import FindingsLedger
from kerkov.core.knowledge_base.models import LedgerSection
from tests.web.inventory_panel_browser_test import browser, dashboard, emit


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
        expect(details).to_contain_text("No recorded route")
    finally:
        kb.close()
