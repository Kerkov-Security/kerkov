"""Exercise network relationships, navigation, and lifecycle in a real browser."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Locator, Page, expect

from tests.web.inventory_panel_browser_test import (
    browser,
    dashboard,
    emit,
    last_request,
    resource,
    sent,
)


def network_fixture() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    entries = [
        resource(
            1,
            section="assets",
            entry={
                "kind": "repository",
                "value": "/workspace/portal",
                "name": "Portal source",
            },
        ),
        resource(
            2,
            entry={
                "kind": "url",
                "value": "https://portal.example.com/",
                "name": "Customer portal",
            },
        ),
        resource(
            3,
            section="assets",
            entry={
                "kind": "domain",
                "value": "portal.example.com",
                "name": "Owned gateway",
            },
        ),
        resource(
            4,
            entry={
                "kind": "domain",
                "value": "portal.example.com",
                "name": "External gateway",
            },
        ),
        resource(
            5,
            entry={
                "kind": "ip",
                "value": "192.0.2.14",
                "name": "Gateway address",
            },
        ),
    ]
    nodes = []
    for node_id, indexes in (
        ("repository:portal", [0]),
        ("url:portal", [1]),
        ("domain:portal", [2, 3]),
        ("ip:gateway", [4]),
    ):
        records = [entries[index] for index in indexes]
        nodes.append(
            {
                "id": node_id,
                "kind": records[0]["entry"]["kind"],
                "value": records[0]["entry"]["value"],
                "roles": list(
                    dict.fromkeys(record["section"][:-1] for record in records)
                ),
                "entry_ids": [record["id"] for record in records],
                "records": [
                    {
                        "entry_id": record["id"],
                        "role": record["section"][:-1],
                        "name": record["entry"]["name"],
                    }
                    for record in records
                ],
            }
        )
    graph = {
        "nodes": nodes,
        "edges": [
            {
                "id": "deployment",
                "source": "repository:portal",
                "target": "url:portal",
                "relation": "deploys_to",
                "provenances": ["declared", "observed"],
                "connection_ids": [6, 7],
                "evidence": [
                    {
                        "connection_id": 6,
                        "provenance": "declared",
                        "artifact_ref": None,
                        "note": "Application deployment configuration",
                    },
                    {
                        "connection_id": 7,
                        "provenance": "observed",
                        "artifact_ref": 42,
                        "note": "Verified deployment fingerprint",
                    },
                ],
            },
            {
                "id": "host",
                "source": "url:portal",
                "target": "domain:portal",
                "relation": "uses_host",
                "provenances": ["derived"],
                "connection_ids": [],
                "evidence": [],
            },
            {
                "id": "dns",
                "source": "domain:portal",
                "target": "ip:gateway",
                "relation": "resolves_to",
                "provenances": ["observed"],
                "connection_ids": [8],
                "evidence": [
                    {
                        "connection_id": 8,
                        "provenance": "observed",
                        "artifact_ref": 43,
                        "note": "DNS answer captured during reconnaissance",
                    }
                ],
            },
        ],
    }
    return entries, graph


def branching_network_fixture() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    entries, graph = network_fixture()
    for entry_id, node_id, kind, value, name in (
        (9, "url:api", "url", "https://api.example.com/v1", "Public API"),
        (10, "domain:api", "domain", "api.example.com", "API gateway"),
        (11, "ip:api", "ip", "192.0.2.25", "API address"),
        (12, "ip:worker", "ip", "192.0.2.36", "Background worker"),
    ):
        entries.append(
            resource(entry_id, entry={"kind": kind, "value": value, "name": name})
        )
        graph["nodes"].append(
            {
                "id": node_id,
                "kind": kind,
                "value": value,
                "roles": ["target"],
                "entry_ids": [entry_id],
                "records": [{"entry_id": entry_id, "role": "target", "name": name}],
            }
        )
    for edge_id, source, target, relation, provenance in (
        ("api-host", "url:api", "domain:api", "uses_host", "derived"),
        ("api-dns", "domain:api", "ip:api", "resolves_to", "observed"),
        ("api-deployment", "repository:portal", "domain:api", "deploys_to", "declared"),
        (
            "worker-deployment",
            "repository:portal",
            "ip:worker",
            "deploys_to",
            "declared",
        ),
    ):
        graph["edges"].append(
            {
                "id": edge_id,
                "source": source,
                "target": target,
                "relation": relation,
                "provenances": [provenance],
                "connection_ids": [],
                "evidence": [],
            }
        )
    return entries, graph


def snapshot(
    page: Page,
    entries: list[dict[str, Any]],
    graph: dict[str, Any],
    *,
    request_id: str = "",
    context_id: str = "context-1",
) -> None:
    emit(
        page,
        "inventory.snapshot",
        {
            "request_id": request_id,
            "context_id": context_id,
            "engagement_id": "engagement-1",
            "available": True,
            "detail": "",
            "entries": entries,
            "graph": graph,
        },
    )


def load_network(
    page: Page,
    entries: list[dict[str, Any]],
    graph: dict[str, Any],
) -> None:
    request = last_request(page, "inventory.list")
    snapshot(page, entries, graph, request_id=request["request_id"])
    page.locator("#network-tab").click()
    expect(page.locator("#network-view")).to_be_visible()
    expect(page.locator("#conversation-view")).to_be_hidden()
    expect(page.locator("#inventory-view")).to_be_hidden()


def node(page: Page, node_id: str) -> Locator:
    return page.locator(f'.network-node[data-node-id="{node_id}"]')


def edge(page: Page, edge_id: str) -> Locator:
    return page.locator(f'.network-edge[data-edge-id="{edge_id}"]')


def test_graph_unifies_roles_and_navigates_to_the_source_record(
    dashboard: Page,
) -> None:
    page = dashboard
    entries, graph = network_fixture()
    load_network(page, entries, graph)
    expect(page.locator(".network-node")).to_have_count(4)
    expect(page.locator(".network-edge")).to_have_count(3)
    gateway = node(page, "domain:portal")
    gateway.focus()
    gateway.press("Enter")
    expect(gateway).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#network-details")).to_contain_text("Owned gateway")
    expect(page.locator(".network-resource-link")).to_have_count(2)
    page.locator(".network-resource-link").filter(has_text="target").click()
    expect(page.locator("#inventory-view")).to_be_visible()
    expect(page.locator("#network-view")).to_be_hidden()
    expect(page.locator("#inventory-detail-name")).to_have_text("External gateway")
    expect(page.locator('button.inventory-row[data-entry-id="4"]')).to_have_attribute(
        "aria-pressed", "true"
    )
    assert sent(page, "user.message") == []


def test_edge_evidence_and_resource_names_are_inert_text(dashboard: Page) -> None:
    page = dashboard
    entries, graph = network_fixture()
    hostile = '<img src=x onerror="window.networkInjected=true">'
    name = f"Portal {hostile}"
    note = f"Observed {hostile}<script>window.networkInjected=true</script>"
    entries[0]["entry"]["name"] = name
    graph["nodes"][0]["records"][0]["name"] = name
    graph["edges"][0]["evidence"][1]["note"] = note
    load_network(page, entries, graph)
    node(page, "repository:portal").click()
    expect(page.locator("#network-details")).to_contain_text(name)
    deployment = edge(page, "deployment")
    deployment.focus()
    deployment.press("Enter")
    expect(page.locator("#network-details")).to_contain_text(note)
    expect(page.locator("#network-details")).to_contain_text(
        "Application deployment configuration"
    )
    expect(page.locator("#network-details")).to_contain_text("42")
    expect(page.locator("#network-view img, #network-view script")).to_have_count(0)
    assert page.evaluate("window.networkInjected === undefined")


def test_filters_keep_only_connections_between_visible_nodes(dashboard: Page) -> None:
    page = dashboard
    entries, graph = network_fixture()
    load_network(page, entries, graph)
    node(page, "repository:portal").click()
    page.locator("#network-role").select_option("target")
    expect(page.locator(".network-node")).to_have_count(3)
    expect(page.locator(".network-edge")).to_have_count(2)
    expect(page.locator(".network-node[aria-pressed=true]")).to_have_count(0)
    page.locator("#network-role").select_option("asset")
    expect(page.locator(".network-node")).to_have_count(2)
    expect(page.locator(".network-edge")).to_have_count(0)
    page.locator("#network-kind").select_option("domain")
    expect(page.locator(".network-node")).to_have_count(1)
    expect(node(page, "domain:portal")).to_be_visible()
    page.locator("#network-role").select_option("all")
    page.locator("#network-kind").select_option("all")
    page.locator("#network-search").fill("customer portal")
    expect(page.locator(".network-node")).to_have_count(1)
    expect(node(page, "url:portal")).to_be_visible()
    expect(page.locator(".network-edge")).to_have_count(0)
    page.locator("#network-search").fill("192.0.2.14")
    expect(node(page, "ip:gateway")).to_be_visible()
    page.locator("#network-search").fill("no such resource")
    expect(page.locator(".network-node")).to_have_count(0)
    expect(page.locator("#network-empty")).to_be_visible()
    page.locator("#network-reset-filters").click()
    expect(page.locator(".network-node")).to_have_count(4)
    expect(page.locator(".network-edge")).to_have_count(3)


def test_connected_resource_navigation_and_selection_survive_refresh(
    dashboard: Page,
) -> None:
    page = dashboard
    entries, graph = network_fixture()
    load_network(page, entries, graph)
    portal = node(page, "url:portal")
    portal.focus()
    portal.press("Space")
    expect(portal).to_have_attribute("aria-pressed", "true")
    page.locator(".network-neighbor").filter(has_text="Owned gateway").click()
    expect(node(page, "domain:portal")).to_have_attribute("aria-pressed", "true")
    snapshot(page, entries, graph)
    expect(node(page, "domain:portal")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#network-details")).to_contain_text("Owned gateway")


def test_inspector_focus_survives_refresh_and_keyboard_neighbor_navigation(
    dashboard: Page,
) -> None:
    page = dashboard
    entries, graph = branching_network_fixture()
    load_network(page, entries, graph)
    node(page, "domain:portal").click()
    target_link = page.locator(".network-resource-link").filter(has_text="target")
    target_link.focus()
    snapshot(page, entries, graph)
    expect(target_link).to_be_focused()
    neighbor = page.locator(".network-neighbor").filter(has_text="Customer portal")
    neighbor.focus()
    neighbor.press("Enter")
    expect(node(page, "url:portal")).to_have_attribute("aria-pressed", "true")
    assert page.evaluate(
        "document.activeElement !== document.body && "
        "document.activeElement.closest('#network-view') !== null"
    ), "Keyboard navigation must keep focus in the visible map"


def test_fit_includes_all_resources_in_a_large_single_kind_graph(
    dashboard: Page,
) -> None:
    page = dashboard
    entries = [
        resource(
            index,
            entry={
                "kind": "domain",
                "value": f"service-{index}.example.com",
                "name": f"Service {index}",
            },
        )
        for index in range(1, 101)
    ]
    graph = {
        "nodes": [
            {
                "id": f"domain:{entry['id']}",
                "kind": "domain",
                "value": entry["entry"]["value"],
                "roles": ["target"],
                "entry_ids": [entry["id"]],
                "records": [
                    {
                        "entry_id": entry["id"],
                        "role": "target",
                        "name": entry["entry"]["name"],
                    }
                ],
            }
            for entry in entries
        ],
        "edges": [],
    }
    load_network(page, entries, graph)
    page.locator("#network-fit").click()
    expect(page.locator(".network-node")).to_have_count(100)
    assert page.evaluate("""() => {
        const viewport = document.querySelector('#network-canvas').getBoundingClientRect();
        return [...document.querySelectorAll('.network-node')].every(node => {
            const box = node.getBoundingClientRect();
            return box.left >= viewport.left - 1 && box.right <= viewport.right + 1
                && box.top >= viewport.top - 1 && box.bottom <= viewport.bottom + 1;
        });
    }"""), "Fit must place every resource inside the map, including large inventories"


def test_empty_map_hides_the_canvas_and_excludes_it_from_keyboard_focus(
    dashboard: Page,
) -> None:
    page = dashboard
    load_network(page, [], {"nodes": [], "edges": []})
    expect(page.locator("#network-empty")).to_be_visible()
    expect(page.locator("#network-canvas")).to_be_hidden()
    assert page.evaluate("""() => {
        const canvas = document.querySelector('#network-canvas');
        canvas.focus();
        return document.activeElement !== canvas;
    }""")


def test_skip_column_connections_do_not_cross_unrelated_resource_cards(
    dashboard: Page,
) -> None:
    page = dashboard
    entries, graph = branching_network_fixture()
    load_network(page, entries, graph)
    assert page.evaluate("""() => {
        const path = document.querySelector(
            '.network-edge[data-edge-id="worker-deployment"] .network-edge-line'
        );
        const matrix = path.getScreenCTM();
        const cards = [...document.querySelectorAll('.network-node')]
            .filter(node => !['repository:portal', 'ip:worker'].includes(node.dataset.nodeId))
            .map(node => node.querySelector('.network-node-card').getBoundingClientRect());
        const length = path.getTotalLength();
        for (let offset = 0; offset <= length; offset += 2) {
            const point = path.getPointAtLength(offset).matrixTransform(matrix);
            if (cards.some(card => point.x > card.left + 2 && point.x < card.right - 2
                && point.y > card.top + 2 && point.y < card.bottom - 2)) return false;
        }
        return true;
    }"""), "A deployment must not appear to connect through an unrelated resource"


def test_pan_zoom_and_long_values_fit_mobile_viewports(dashboard: Page) -> None:
    page = dashboard
    entries, graph = network_fixture()
    graph["nodes"][0]["value"] = "/workspace/" + "long-path" * 80
    graph["nodes"][0]["records"][0]["name"] = "Repository " + "x" * 200
    load_network(page, entries, graph)
    world = page.locator("#network-world")
    original = world.get_attribute("transform")
    page.locator("#network-zoom-in").click()
    assert world.get_attribute("transform") != original
    page.locator("#network-fit").click()
    fitted = world.get_attribute("transform")
    canvas = page.locator("#network-canvas").bounding_box()
    assert canvas is not None
    start = (canvas["x"] + 20, canvas["y"] + 20)
    page.mouse.move(*start)
    page.mouse.down()
    page.mouse.move(start[0] + 90, start[1] + 70, steps=5)
    page.mouse.up()
    assert world.get_attribute("transform") != fitted
    node(page, "repository:portal").click()
    for width in (390, 320):
        page.set_viewport_size({"width": width, "height": 844})
        expect(page.locator("#network-details")).to_be_visible()
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        ), f"Network map overflows the {width}px viewport"


@pytest.mark.parametrize(
    ("event", "payload"),
    [
        ("session.cleared", {}),
        ("mode.changed", {"to": "research", "name": "Research", "description": ""}),
    ],
)
def test_context_reset_discards_network_and_late_responses(
    dashboard: Page, event: str, payload: dict[str, Any]
) -> None:
    page = dashboard
    entries, graph = network_fixture()
    load_network(page, entries, graph)
    node(page, "domain:portal").click()
    old_request = last_request(page, "inventory.list")
    snapshot(page, [], {"nodes": [], "edges": []}, context_id="context-2")
    emit(page, event, payload)
    expect(page.locator(".network-node")).to_have_count(0)
    snapshot(page, entries, graph, request_id=old_request["request_id"])
    snapshot(page, entries, graph)
    expect(page.locator(".network-node")).to_have_count(0)
    expect(page.locator(".network-edge")).to_have_count(0)
    expect(page.locator("#network-details")).not_to_contain_text("Owned gateway")


def test_disconnect_preserves_map_and_approval_returns_to_conversation(
    dashboard: Page,
) -> None:
    page = dashboard
    entries, graph = network_fixture()
    load_network(page, entries, graph)
    node(page, "domain:portal").click()
    emit(
        page,
        "approval.request",
        {
            "approval_id": "approval-1",
            "tool": "read_file",
            "params": {"path": "/workspace/portal/config.py"},
        },
    )
    expect(page.locator("#conversation-view")).to_be_visible()
    expect(page.locator("#network-view")).to_be_hidden()
    expect(page.locator("#approval-tray")).to_be_visible()
    expect(page.locator("#approval-list .activity-title")).to_be_focused()
    page.locator("#network-tab").click()
    expect(node(page, "domain:portal")).to_have_attribute("aria-pressed", "true")
    page.evaluate("window.testSocket.close()")
    expect(page.locator(".network-node")).to_have_count(4)
    expect(page.locator("#network-details")).to_contain_text("Owned gateway")
    expect(page.locator("#network-refresh")).to_be_disabled()
    assert sent(page, "approval.response") == []


def test_refresh_preserves_zoom_and_pan(dashboard: Page) -> None:
    page = dashboard
    entries, graph = network_fixture()
    load_network(page, entries, graph)
    page.locator("#network-zoom-in").click()
    page.locator("#network-canvas").focus()
    page.locator("#network-canvas").press("ArrowLeft")
    world = page.locator("#network-world")
    camera = world.get_attribute("transform")
    page.locator("#network-refresh").click()
    # Allow both status-driven ResizeObserver callbacks to run.
    page.wait_for_timeout(100)
    expect(world).to_have_attribute("transform", camera)
    request = last_request(page, "inventory.list")
    snapshot(page, entries, graph, request_id=request["request_id"])
    page.wait_for_timeout(100)
    expect(world).to_have_attribute("transform", camera)


@pytest.mark.parametrize("empty", [False, True])
def test_map_inventory_links_preserve_unsaved_form(
    dashboard: Page, empty: bool
) -> None:
    page = dashboard
    entries, graph = network_fixture()
    load_network(
        page, [] if empty else entries, {"nodes": [], "edges": []} if empty else graph
    )
    page.locator("#inventory-tab").click()
    page.locator("#inventory-add").click()
    page.locator("#inventory-name").fill("Unsaved resource")
    page.locator("#network-tab").click()
    if empty:
        page.locator("#network-add-resource").click()
    else:
        node(page, "domain:portal").click()
        page.locator(".network-resource-link").first.click()
    expect(page.locator("#inventory-form")).to_be_visible()
    expect(page.locator("#inventory-name")).to_have_value("Unsaved resource")
