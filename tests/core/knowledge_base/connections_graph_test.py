"""Connection semantics and graph projection independent of persistence/UI."""

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from kerkov.core.knowledge_base.connections import (
    SQLITE_MAX_ID,
    Connection,
    project_resource_graph,
    validate_connection_resources,
)
from kerkov.core.knowledge_base.resources import Resource


def assertion(**changes):
    return {
        "source_id": 1,
        "target_id": 2,
        "relation": "resolves_to",
        "provenance": "declared",
        "note": "Confirmed by the owner",
        **changes,
    }


def row(entry_id, section, entry, artifact_ref=None):
    return SimpleNamespace(
        id=entry_id, section=section, entry=entry, artifact_ref=artifact_ref
    )


def resource(entry_id, section, kind, value, **extra):
    return row(entry_id, section, {"kind": kind, "value": value, **extra})


@pytest.mark.parametrize(
    "changes",
    [
        {"source_id": True},
        {"source_id": "1"},
        {"target_id": 2.0},
        {"source_id": 0},
        {"target_id": -1},
        {"target_id": 1 << 63},
        {"target_id": 1},
        {"relation": "uses_host"},
        {"provenance": "inferred"},
        {"note": None},
        {"note": " "},
        {"note": "one\ntwo"},
        {"note": "a" * 2001},
        {"note": 123},
        {"unknown": "field"},
    ],
)
def test_invalid_assertions_are_rejected(changes):
    with pytest.raises(ValidationError):
        Connection.model_validate(assertion(**changes))


def test_provenance_and_note_contract():
    observed = Connection.model_validate(assertion(provenance="observed", note=None))
    assert observed.note is None  # The database checks the artifact separately.
    declared = Connection.model_validate(assertion(note="  Owner confirmed  "))
    assert declared.note == "Owner confirmed"


@pytest.mark.parametrize(
    "relation,source,target,valid",
    [
        ("resolves_to", ("domain", "api.example"), ("ip", "192.0.2.1"), True),
        ("resolves_to", ("ip", "192.0.2.1"), ("domain", "api.example"), False),
        ("resolves_to", ("domain", "api.example"), ("domain", "api.example"), False),
        (
            "deploys_to",
            ("repository", "/work/api"),
            ("url", "https://api.example"),
            True,
        ),
        ("deploys_to", ("repository", "/work/api"), ("domain", "api.example"), True),
        ("deploys_to", ("repository", "/work/api"), ("ip", "192.0.2.1"), True),
        ("deploys_to", ("repository", "/work/api"), ("repository", "/work/web"), False),
        ("deploys_to", ("url", "https://api.example"), ("ip", "192.0.2.1"), False),
    ],
)
def test_relation_endpoint_semantics(relation, source, target, valid):
    connection = Connection.model_validate(assertion(relation=relation))
    endpoints = [Resource(kind=kind, value=value) for kind, value in (source, target)]
    if valid:
        validate_connection_resources(connection, *endpoints)
    else:
        with pytest.raises(ValueError):
            validate_connection_resources(connection, *endpoints)


def test_roles_and_connection_evidence_coalesce_without_losing_names():
    rows = [
        resource(1, "assets", "domain", "API.Example.", name="Production API"),
        resource(2, "targets", "domain", "api.example", name="Testing scope"),
        resource(3, "targets", "ip", "192.0.2.1"),
        row(4, "connections", assertion(target_id=3), artifact_ref=None),
        row(
            5,
            "connections",
            assertion(source_id=2, target_id=3, provenance="observed", note=None),
            artifact_ref=12,
        ),
    ]
    graph = project_resource_graph(rows)
    domain, ip = graph["nodes"]
    assert domain["roles"] == ["asset", "target"]
    assert domain["entry_ids"] == [1, 2]
    assert domain["records"] == [
        {"entry_id": 1, "role": "asset", "name": "Production API"},
        {"entry_id": 2, "role": "target", "name": "Testing scope"},
    ]
    assert ip["roles"] == ["target"]
    [edge] = graph["edges"]
    assert (edge["source"], edge["target"]) == (domain["id"], ip["id"])
    assert edge["connection_ids"] == [4, 5]
    assert edge["provenances"] == ["declared", "observed"]
    assert edge["evidence"] == [
        {
            "connection_id": 4,
            "provenance": "declared",
            "artifact_ref": None,
            "note": "Confirmed by the owner",
        },
        {
            "connection_id": 5,
            "provenance": "observed",
            "artifact_ref": 12,
            "note": None,
        },
    ]
    assert project_resource_graph(reversed(rows)) == graph
    assert json.loads(json.dumps(graph)) == graph


def test_ids_survive_role_and_name_changes_and_do_not_depend_on_ledger_ids():
    first = project_resource_graph(
        [resource(1, "assets", "domain", "api.example", name="Old name")]
    )
    second = project_resource_graph(
        [resource(12, "targets", "domain", "API.Example.", name="New name")]
    )
    assert first["nodes"][0]["id"] == second["nodes"][0]["id"]


def test_url_host_edges_require_exact_existing_resources_and_never_infer_dns():
    graph = project_resource_graph(
        [
            resource(1, "assets", "url", "https://API.Example:8443/path?q=Foo"),
            resource(2, "targets", "domain", "api.example"),
            resource(3, "assets", "url", "http://[2001:0db8::1]/api"),
            resource(4, "assets", "ip", "2001:db8::1"),
            resource(5, "assets", "url", "http://192.0.2.1/path"),
            resource(6, "targets", "ip", "192.0.2.1"),
            resource(7, "targets", "url", "https://unlisted.api.example"),
            resource(8, "assets", "domain", "example"),
        ]
    )
    assert len(graph["nodes"]) == 8
    assert len(graph["edges"]) == 3
    by_id = {node["id"]: node for node in graph["nodes"]}
    assert {
        (by_id[edge["source"]]["value"], by_id[edge["target"]]["value"])
        for edge in graph["edges"]
    } == {
        ("https://api.example:8443/path?q=Foo", "api.example"),
        ("http://[2001:db8::1]/api", "2001:db8::1"),
        ("http://192.0.2.1/path", "192.0.2.1"),
    }
    for edge in graph["edges"]:
        assert edge["relation"] == "uses_host"
        assert edge["provenances"] == ["derived"]
        assert edge["connection_ids"] == edge["evidence"] == []


def test_unrelated_sensitive_invalid_and_dangling_rows_are_excluded():
    graph = project_resource_graph(
        [
            resource(1, "assets", "domain", "api.example"),
            resource(2, "targets", "ip", "192.0.2.1"),
            row(3, "credentials", {"password": "secret"}),
            row(4, "findings", {"title": "internal finding"}),
            row(5, "hosts", {"ip": "192.0.2.2"}),
            row(6, "assets", {"kind": "ip", "value": "invalid"}),
            row(7, "connections", assertion(target_id=99)),
            row(8, "connections", assertion(target_id=3)),
            row(9, "connections", assertion(source_id=2, target_id=1)),
            row(10, "connections", assertion(provenance="observed", note=None)),
            row(11, "connections", {"unknown": "secret"}),
            resource(None, "assets", "domain", "unsaved.example"),
        ]
    )
    assert len(graph["nodes"]) == 2
    assert graph["edges"] == []
    assert "secret" not in json.dumps(graph)
    assert "internal finding" not in json.dumps(graph)


def test_graph_does_not_mutate_input_rows():
    rows = [resource(1, "assets", "domain", "API.Example.")]
    before = json.dumps(vars(rows[0]))
    project_resource_graph(rows)
    assert json.dumps(vars(rows[0])) == before


@pytest.mark.parametrize("entry_id", [None, True, 0, -1, "1", 1.0, SQLITE_MAX_ID + 1])
def test_graph_omits_resources_without_valid_persisted_integer_ids(entry_id):
    graph = project_resource_graph(
        [
            resource(entry_id, "assets", "domain", "invalid-id.example"),
            resource(2, "targets", "ip", "192.0.2.1"),
        ]
    )
    assert len(graph["nodes"]) == 1
    assert graph["nodes"][0]["entry_ids"] == [2]
    assert graph["edges"] == []


def test_repository_graph_identity_survives_filesystem_changes(tmp_path):
    planned = tmp_path / "planned"
    actual = tmp_path / "actual"
    persisted = Resource(kind="repository", value=str(planned)).model_dump()
    rows = [row(1, "assets", persisted)]
    original = project_resource_graph(rows)
    actual.mkdir()
    planned.symlink_to(actual, target_is_directory=True)
    assert project_resource_graph(rows) == original
    assert Resource.from_stored(persisted).value == str(planned)
    assert Resource(kind="repository", value=str(planned)).value == str(actual)


@pytest.mark.parametrize("value", ["relative/path", "~/repo"])
def test_saved_repository_identity_must_already_be_absolute(value):
    with pytest.raises(ValidationError):
        Resource.from_stored({"kind": "repository", "value": value})
    assert project_resource_graph([resource(1, "assets", "repository", value)]) == {
        "nodes": [],
        "edges": [],
    }


def container_rows():
    from kerkov.core.knowledge_base.containers import ContainerObservation

    rows = [resource(1, "targets", "ip", "127.0.0.1")]
    for index, (name, port) in enumerate(
        [("Flask", 8000), ("ActiveMQ", 8161), ("Neo4j", 7474)]
    ):
        target_id = 2 + index
        rows.append(
            resource(
                target_id, "targets", "url", f"http://127.0.0.1:{port}/", name=name
            )
        )
        observation = ContainerObservation(
            compose_dir=f"/labs/{name}",
            project_name="",
            name=name,
            service="web",
            state="running",
            health="healthy",
            ready=True,
            observed_at="2026-10-11T12:00:00+00:00",
            readiness_basis="HTTP probe ready",
            log_artifact="/artifacts/start.log",
            ports=[
                {
                    "host": "0.0.0.0",
                    "host_port": port,
                    "container_port": 80,
                    "protocol": "tcp",
                }
            ],
            target_ids=[1, target_id],
        )
        rows.append(row(10 + index, "containers", observation.model_dump()))
    return rows


def test_containers_remain_distinct_on_localhost_without_inventing_access_routes():
    rows = container_rows()
    graph = project_resource_graph(rows)
    assert graph == project_resource_graph(reversed(rows))
    containers = [node for node in graph["nodes"] if node["kind"] == "container"]
    assert len(containers) == 3
    assert len({node["id"] for node in containers}) == 3
    assert all(len(node["endpoint_ids"]) == 2 for node in containers)
    assert all(node["container"]["ready"] for node in containers)
    assert all(edge["relation"] == "uses_host" for edge in graph["edges"])
    rows[-1].entry.update(state="stopped", ready=False, target_ids=[999])
    stopped_graph = project_resource_graph(rows)
    stopped = next(
        node
        for node in stopped_graph["nodes"]
        if node["value"] == "Neo4j" and node["kind"] == "container"
    )
    original = next(node for node in containers if node["value"] == "Neo4j")
    assert stopped["id"] == original["id"] and stopped["endpoint_ids"] == []
    assert stopped["container"]["ready"] is False
    rows.append(row(99, "containers", {"name": "invalid"}))
    assert project_resource_graph(rows) == stopped_graph
