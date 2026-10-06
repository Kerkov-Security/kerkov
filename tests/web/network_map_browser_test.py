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


def add_node(entries, graph, entry_id, node_id, kind, value, name):
    entry = resource(entry_id, entry={"kind": kind, "value": value, "name": name})
    entries.append(entry)
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


def access_fixture() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    entries, graph = network_fixture()
    graph["nodes"].append(
        {
            "id": "vantage:runner",
            "kind": "vantage",
            "value": "Kerkov runner",
            "roles": [],
            "entry_ids": [],
            "records": [],
        }
    )
    add_node(entries, graph, 9, "domain:db", "domain", "db.internal", "Database")
    add_node(
        entries, graph, 10, "domain:api", "domain", "api.example.com", "Public API"
    )
    add_node(entries, graph, 11, "ip:worker", "ip", "192.0.2.36", "Worker")
    add_node(
        entries, graph, 12, "url:solo", "url", "https://solo.example.com/docs", "Docs"
    )
    graph["edges"].append(
        {
            "id": "shared-dns",
            "source": "domain:api",
            "target": "ip:gateway",
            "relation": "resolves_to",
            "provenances": ["observed"],
            "connection_ids": [],
            "evidence": [],
        }
    )
    for index, (source, target, protocol, port, service, provenance) in enumerate(
        [
            ("vantage:runner", "domain:portal", "tcp", 443, "HTTPS", "observed"),
            ("vantage:runner", "domain:portal", "tcp", 22, "SSH", "declared"),
            ("domain:portal", "domain:db", "tcp", 5432, "PostgreSQL", "declared"),
            ("domain:db", "domain:portal", "tcp", 443, "HTTPS", "observed"),
        ]
    ):
        observed_at = "2026-10-05T16:00:00+00:00" if provenance == "observed" else None
        graph["edges"].append(
            {
                "id": f"access-{index}",
                "source": source,
                "target": target,
                "relation": "reaches",
                "protocol": protocol,
                "port": port,
                "service": service,
                "observed_at": observed_at,
                "provenances": [provenance],
                "connection_ids": [20 + index],
                "evidence": [
                    {
                        "connection_id": 20 + index,
                        "provenance": provenance,
                        "artifact_ref": 42 if provenance == "observed" else None,
                        "note": f"Evidence for {service}",
                        "observed_at": observed_at,
                    }
                ],
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


def edge(page: Page, source: str, target: str) -> Locator:
    return page.locator(".network-edge").filter(
        has=page.locator("title", has_text=f"{source} to {target},")
    )


def test_hosts_group_endpoints_and_keep_dns_and_repositories_in_details(
    dashboard: Page,
) -> None:
    page = dashboard
    entries, graph = network_fixture()
    load_network(page, entries, graph)
    expect(page.locator(".network-node")).to_have_count(1)
    expect(page.locator(".network-edge")).to_have_count(0)
    portal = node(page, "host:portal.example.com")
    portal.focus()
    portal.press("Enter")
    expect(portal).to_have_attribute("aria-pressed", "true")
    expect(portal).to_contain_text("No recorded route")
    expect(page.locator("#network-details")).to_contain_text("Owned gateway")
    expect(page.locator("#network-details")).to_contain_text("192.0.2.14")
    expect(page.locator("#network-details")).to_contain_text(
        "Verified deployment fingerprint"
    )
    expect(page.locator("#network-details")).to_contain_text("DNS answer captured")
    expect(page.locator("#network-details .network-endpoint-link")).to_have_attribute(
        "href", "https://portal.example.com/"
    )
    expect(page.locator("#network-details .network-endpoint-link")).to_have_attribute(
        "rel", "noopener noreferrer"
    )
    expect(page.locator("#network-repositories")).to_contain_text("Portal source")
    page.locator('#network-details [data-focus-key="resource:4"]').click()
    expect(page.locator("#inventory-view")).to_be_visible()
    expect(page.locator("#inventory-detail-name")).to_have_text("External gateway")
    assert sent(page, "user.message") == []


def test_shared_dns_never_merges_hosts_and_ip_routes_keep_ip_card(
    dashboard: Page,
) -> None:
    page = dashboard
    entries, graph = access_fixture()
    load_network(page, entries, graph)
    expect(page.locator(".network-node")).to_have_count(
        6
    )  # Five hosts, one explicit origin.
    expect(node(page, "host:portal.example.com")).to_be_visible()
    expect(node(page, "host:api.example.com")).to_be_visible()
    expect(node(page, "host:192.0.2.14")).to_have_count(0)
    expect(node(page, "host:192.0.2.36")).to_be_visible()
    expect(node(page, "host:solo.example.com")).to_be_visible()
    graph["edges"][-1]["target"] = "ip:gateway"
    snapshot(page, entries, graph)
    expect(node(page, "host:192.0.2.14")).to_be_visible()
    expect(node(page, "host:portal.example.com")).to_be_visible()


def test_exact_normalization_and_vantage_identity(dashboard: Page) -> None:
    page = dashboard
    entries, graph = network_fixture()
    add_node(
        entries,
        graph,
        15,
        "url:cased",
        "url",
        "https://PORTAL.example.com.:8443/admin",
        "Admin",
    )
    add_node(entries, graph, 16, "ip:v6", "ip", "2001:db8::1", "IPv6")
    add_node(entries, graph, 17, "url:v6", "url", "https://[2001:db8::1]/", "IPv6 URL")
    add_node(entries, graph, 18, "domain:numeric", "domain", "127.1", "Numeric DNS")
    add_node(
        entries, graph, 19, "url:numeric", "url", "https://127.1/", "Numeric DNS URL"
    )
    add_node(entries, graph, 21, "ip:loopback", "ip", "127.0.0.1", "Separate IP")
    graph["nodes"].append(
        {
            "id": "vantage:same-name",
            "kind": "vantage",
            "value": "portal.example.com",
            "roles": [],
            "entry_ids": [],
            "records": [],
        }
    )
    load_network(page, entries, graph)
    expect(page.locator(".network-node")).to_have_count(5)
    expect(node(page, "host:127.1")).to_contain_text("1 URL")
    expect(node(page, "host:127.0.0.1")).to_be_visible()
    expect(node(page, "host:portal.example.com")).to_contain_text("2 URLs")
    expect(node(page, "host:2001:db8::1")).to_contain_text("1 URL")
    expect(node(page, "vantage:same-name")).to_be_visible()


def test_services_bundle_by_direction_and_keep_evidence(dashboard: Page) -> None:
    page = dashboard
    entries, graph = access_fixture()
    load_network(page, entries, graph)
    expect(page.locator(".network-edge")).to_have_count(3)
    route = edge(page, "Kerkov runner", "portal.example.com")
    expect(route).to_contain_text("HTTPS :443 +1")
    expect(route).to_have_class("network-edge provenance-declared")
    route.focus()
    route.press("Enter")
    expect(page.locator("#network-details")).to_contain_text("SSH :22")
    expect(page.locator("#network-details")).to_contain_text("Declared · unverified")
    expect(page.locator("#network-details")).to_contain_text(
        "2026-10-05T16:00:00+00:00"
    )
    expect(page.locator("#network-details")).to_contain_text("artifact #42")
    expect(edge(page, "portal.example.com", "db.internal")).to_have_count(1)
    expect(edge(page, "db.internal", "portal.example.com")).to_have_class(
        "network-edge provenance-observed is-dimmed"
    )


def test_bundle_counts_transport_distinct_services_and_supports_pointer_selection(
    dashboard: Page,
) -> None:
    page = dashboard
    entries, graph = access_fixture()
    udp = dict(graph["edges"][-4], id="access-udp", protocol="udp")
    graph["edges"].append(udp)
    load_network(page, entries, graph)
    route = edge(page, "Kerkov runner", "portal.example.com")
    expect(route).to_contain_text("HTTPS :443 +2")
    route.locator(".network-edge-label").click()
    expect(page.locator("#network-details")).to_contain_text("TCP · Observed")
    expect(page.locator("#network-details")).to_contain_text("UDP · Observed")


def test_origin_filter_shows_direct_routes_without_inferred_transit(
    dashboard: Page,
) -> None:
    page = dashboard
    entries, graph = access_fixture()
    load_network(page, entries, graph)
    expect(page.locator("#network-origin")).to_have_value("all")
    page.locator("#network-origin").select_option("vantage:runner")
    expect(page.locator(".network-edge")).to_have_count(1)
    expect(edge(page, "Kerkov runner", "portal.example.com")).to_be_visible()
    expect(node(page, "host:db.internal")).to_contain_text("No recorded route")
    node(page, "host:db.internal").click()
    expect(page.locator("#network-details")).to_contain_text(
        "No direct route from Kerkov runner"
    )
    page.locator("#network-origin").select_option("host:portal.example.com")
    expect(page.locator(".network-edge")).to_have_count(1)
    expect(edge(page, "portal.example.com", "db.internal")).to_be_visible()


def test_search_and_resource_filters_keep_direct_origins_as_context(
    dashboard: Page,
) -> None:
    page = dashboard
    entries, graph = access_fixture()
    load_network(page, entries, graph)
    page.locator("#network-role").select_option("target")
    expect(node(page, "vantage:runner")).to_be_visible()
    expect(page.locator(".network-edge")).to_have_count(3)
    page.locator("#network-kind").select_option("url")
    expect(node(page, "host:portal.example.com")).to_be_visible()
    expect(node(page, "vantage:runner")).to_be_visible()
    page.locator("#network-search").fill("customer portal")
    expect(page.locator(".network-node")).to_have_count(
        3
    )  # Match and its two direct origins.
    page.locator("#network-kind").select_option("all")
    page.locator("#network-search").fill("192.0.2.14")
    expect(node(page, "host:api.example.com")).to_be_visible()
    expect(node(page, "host:portal.example.com")).to_be_visible()
    page.locator("#network-search").fill("no such host")
    expect(page.locator("#network-empty")).to_be_visible()
    page.locator("#network-reset-filters").click()
    expect(page.locator(".network-node")).to_have_count(6)


def test_unattached_repository_remains_available_and_searchable(
    dashboard: Page,
) -> None:
    page = dashboard
    entries, graph = network_fixture()
    graph["edges"] = []
    load_network(page, entries, graph)
    page.locator("#network-kind").select_option("repository")
    expect(page.locator("#network-empty")).to_be_visible()
    page.locator("#network-repositories summary").click()
    expect(page.locator("#network-repositories")).to_contain_text("No associated host")
    page.locator("#network-repositories .network-resource-link").click()
    expect(page.locator("#inventory-detail-name")).to_have_text("Portal source")


def test_edge_evidence_and_resource_names_are_inert_text(dashboard: Page) -> None:
    page = dashboard
    entries, graph = access_fixture()
    hostile = '<img src=x onerror="window.networkInjected=true">'
    name = f"Portal {hostile}"
    note = f"Observed {hostile}<script>window.networkInjected=true</script>"
    entries[0]["entry"]["name"] = name
    graph["nodes"][0]["records"][0]["name"] = name
    graph["edges"][-4]["evidence"][0]["note"] = note
    load_network(page, entries, graph)
    node(page, "host:portal.example.com").click()
    expect(page.locator("#network-details")).to_contain_text(name)
    route = edge(page, "Kerkov runner", "portal.example.com")
    route.focus()
    route.press("Enter")
    expect(page.locator("#network-details")).to_contain_text(note)
    expect(page.locator("#network-view img, #network-view script")).to_have_count(0)
    assert page.evaluate("window.networkInjected === undefined")


def test_inspector_focus_selection_and_neighbor_navigation_survive_refresh(
    dashboard: Page,
) -> None:
    page = dashboard
    entries, graph = access_fixture()
    load_network(page, entries, graph)
    portal = node(page, "host:portal.example.com")
    portal.focus()
    portal.press("Space")
    target_link = page.locator('#network-details [data-focus-key="resource:4"]')
    target_link.focus()
    snapshot(page, entries, graph)
    expect(target_link).to_be_focused()
    expect(portal).to_have_attribute("aria-pressed", "true")
    neighbor = page.locator(".network-neighbor").filter(has_text="Kerkov runner")
    neighbor.focus()
    neighbor.press("Enter")
    expect(node(page, "vantage:runner")).to_have_attribute("aria-pressed", "true")
    assert page.evaluate("document.activeElement.closest('#network-view') !== null")
    page.locator("#network-canvas").focus()
    page.locator("#network-canvas").press("Escape")
    expect(page.locator("#network-details")).to_be_hidden()


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


def test_routes_never_pass_through_unrelated_host_cards(dashboard: Page) -> None:
    page = dashboard
    entries, graph = access_fixture()
    graph["edges"].append(
        dict(graph["edges"][-4], id="across-row", target="domain:api")
    )
    load_network(page, entries, graph)
    assert page.evaluate(
        """() => [...document.querySelectorAll('.network-edge')].every(edge => {
        const path = edge.querySelector('.network-edge-line');
        const matrix = path.getScreenCTM();
        const cards = [...document.querySelectorAll('.network-node')]
            .filter(node => ![edge.dataset.source, edge.dataset.target].includes(node.dataset.nodeId))
            .map(node => node.querySelector('.network-node-card').getBoundingClientRect());
        const length = path.getTotalLength();
        for (let offset = 0; offset <= length; offset += 2) {
            const point = path.getPointAtLength(offset).matrixTransform(matrix);
            if (cards.some(card => point.x > card.left + 2 && point.x < card.right - 2
                && point.y > card.top + 2 && point.y < card.bottom - 2)) return false;
        }
        return true;
    })"""
    )


def test_mobile_reverse_routes_have_separate_labels(dashboard: Page) -> None:
    page = dashboard
    entries, graph = access_fixture()
    page.set_viewport_size({"width": 390, "height": 844})
    load_network(page, entries, graph)
    forward = edge(page, "portal.example.com", "db.internal").locator(
        ".network-edge-label"
    )
    reverse = edge(page, "db.internal", "portal.example.com").locator(
        ".network-edge-label"
    )
    first, second = forward.bounding_box(), reverse.bounding_box()
    assert first is not None and second is not None
    assert (
        first["y"] + first["height"] <= second["y"]
        or second["y"] + second["height"] <= first["y"]
    )
    forward.click()
    expect(page.locator("#network-details")).to_contain_text("PostgreSQL :5432")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_fit_includes_long_route_labels_on_a_two_host_mobile_map(
    dashboard: Page,
) -> None:
    page = dashboard
    entries, graph = access_fixture()
    ids = {"vantage:runner", "domain:portal"}
    graph["nodes"] = [item for item in graph["nodes"] if item["id"] in ids]
    graph["edges"] = [
        item
        for item in graph["edges"]
        if item["source"] in ids and item["target"] in ids
    ]
    graph["edges"][0]["service"] = "Long enterprise gateway service name"
    page.set_viewport_size({"width": 320, "height": 844})
    load_network(page, entries, graph)
    page.locator("#network-fit").click()
    expect(page.locator(".network-node")).to_have_count(2)
    expect(page.locator(".network-edge")).to_have_count(1)
    assert page.evaluate("""() => {
        const viewport = document.querySelector('#network-canvas').getBoundingClientRect();
        return [...document.querySelectorAll('.network-node, .network-edge-label, .network-edge-line')].every(element => {
            const box = element.getBoundingClientRect();
            return box.left >= viewport.left && box.right <= viewport.right
                && box.top >= viewport.top && box.bottom <= viewport.bottom;
        });
    }"""), "Fit must include route labels and gutter paths, not just host cards"
    page.locator(".network-edge-label").click()
    expect(page.locator("#network-details")).to_contain_text(
        "Long enterprise gateway service name"
    )


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
    node(page, "host:portal.example.com").click()
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
    node(page, "host:portal.example.com").click()
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
    node(page, "host:portal.example.com").click()
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
    expect(node(page, "host:portal.example.com")).to_have_attribute(
        "aria-pressed", "true"
    )
    page.evaluate("window.testSocket.close()")
    expect(page.locator(".network-node")).to_have_count(1)
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
        node(page, "host:portal.example.com").click()
        page.locator("#network-details .network-resource-link").first.click()
    expect(page.locator("#inventory-form")).to_be_visible()
    expect(page.locator("#inventory-name")).to_have_value("Unsaved resource")
