"""Admission and offline execution of reusable upstream-style InSpec projects."""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from kerkov.core.compliance.cinc_report import parse_report
from kerkov.core.compliance.cinc_runtime import profile_metadata, snapshot_profile
from kerkov.core.compliance.profile_bundle import inspect_profile_bundle


def _write_document(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value))


def _create_mock_bundle(root):
    digest = "1" * 64
    _write_document(
        root / "inspec.yml",
        {
            "name": "upstream-project",
            "version": "2.0.0",
            "depends": [{"name": "shared", "git": "https://example.invalid/shared"}],
            "inputs": [{"name": "secret", "value": "private-input-default"}],
        },
    )
    lock = {
        "lockfile_version": 1,
        "depends": [
            {
                "name": "shared",
                "resolved_source": {
                    "path": "/unavailable/controller/checkout",
                    "sha256": digest,
                },
                "version_constraints": [],
            }
        ],
    }
    _write_document(root / "inspec.lock", lock)
    child = root / "vendor" / digest
    _write_document(
        child / "inspec.yml", {"name": "resource-project", "version": "1.0.0"}
    )
    (child / "libraries").mkdir()
    (child / "libraries/shared.rb").write_text(
        "class ExternalProjectValue < Inspec.resource(1)\n"
        "  name 'external_project_value'\n"
        "  def value\n    'upstream-fixture'\n  end\nend\n"
    )
    (root / "controls").mkdir()
    (root / "controls/check.rb").write_text(
        "control 'upstream-control' do\n  impact 0.5\n"
        "  title 'Use the upstream shared resource'\n"
        "  desc 'Verify the bundled dependency loads without its original source.'\n"
        "  describe external_project_value do\n"
        "    its('value') { should eq 'upstream-fixture' }\n  end\nend\n"
    )
    return child, lock


def test_self_contained_projects_can_have_empty_optional_dependencies(tmp_path):
    _write_document(tmp_path / "inspec.yml", {"name": "standalone", "depends": None})
    metadata, dependencies = inspect_profile_bundle(tmp_path)
    assert metadata["name"] == "standalone"
    assert dependencies == []


def test_locked_project_keeps_upstream_metadata_and_does_not_expose_inputs(tmp_path):
    _create_mock_bundle(tmp_path)
    metadata, dependencies = inspect_profile_bundle(tmp_path)
    assert profile_metadata(tmp_path) == metadata
    assert metadata["name"] == "upstream-project"
    assert dependencies == [
        {
            "name": "shared",
            "profile_name": "resource-project",
            "version": "1.0.0",
            "cache_key": "1" * 64,
        }
    ]
    assert "private-input-default" not in json.dumps(dependencies)
    before = snapshot_profile(tmp_path, None, tmp_path, set())
    (tmp_path / "vendor" / ("1" * 64) / "libraries/shared.rb").write_text("changed")
    assert snapshot_profile(tmp_path, None, tmp_path, set()) != before


@pytest.mark.parametrize("failure", ["lock", "vendor", "cache-lock", "archive", "gems"])
def test_incomplete_bundles_and_gem_dependencies_fail_before_execution(
    tmp_path, failure
):
    child, _ = _create_mock_bundle(tmp_path)
    if failure == "lock":
        (tmp_path / "inspec.lock").unlink()
    elif failure == "vendor":
        (child / "inspec.yml").unlink()
    elif failure == "cache-lock":
        (child / ".lock").touch()
    elif failure == "archive":
        child.rename(tmp_path / "vendor/unpacked-elsewhere")
        (tmp_path / "vendor" / (("1" * 64) + ".tar.gz")).write_bytes(b"archive")
    else:
        _write_document(
            child / "inspec.yml",
            {"name": "shared", "gem_dependencies": [{"name": "example"}]},
        )
    with pytest.raises(ValueError):
        inspect_profile_bundle(tmp_path)


def test_dependency_caches_cannot_override_the_project_cache(tmp_path):
    child, _ = _create_mock_bundle(tmp_path)
    (child / "vendor").mkdir()
    with pytest.raises(ValueError, match="Flatten"):
        inspect_profile_bundle(tmp_path)


def test_dependency_count_is_bounded(tmp_path, monkeypatch):
    _create_mock_bundle(tmp_path)
    monkeypatch.setattr("kerkov.core.compliance.profile_bundle.MAX_DEPENDENCIES", 0)
    with pytest.raises(ValueError):
        inspect_profile_bundle(tmp_path)


@pytest.mark.parametrize("value", ["../escape", "gem:resource:1.0", "0" * 63, None])
def test_vendor_keys_cannot_escape_the_bundle_or_load_gem_resources(tmp_path, value):
    _, lock = _create_mock_bundle(tmp_path)
    lock["depends"][0]["resolved_source"]["sha256"] = value
    _write_document(tmp_path / "inspec.lock", lock)
    with pytest.raises(ValueError, match="locked SHA-256"):
        inspect_profile_bundle(tmp_path)


@pytest.mark.parametrize(
    "change", ["undeclared", "duplicate", "bad-version", "bad-source", "bad-shape"]
)
def test_lockfiles_must_describe_the_declared_dependency_graph(tmp_path, change):
    _, lock = _create_mock_bundle(tmp_path)
    if change == "undeclared":
        lock["depends"][0]["name"] = "unexpected"
    elif change == "duplicate":
        lock["depends"] *= 2
    elif change == "bad-version":
        lock["lockfile_version"] = True
    elif change == "bad-source":
        lock["depends"][0]["resolved_source"]["gem"] = "example"
    else:
        lock["depends"] = {}
    _write_document(tmp_path / "inspec.lock", lock)
    with pytest.raises(ValueError):
        inspect_profile_bundle(tmp_path)


def test_transitive_dependencies_are_checked_and_cycles_rejected(tmp_path, monkeypatch):
    child, lock = _create_mock_bundle(tmp_path)
    _write_document(
        child / "inspec.yml", {"name": "shared", "depends": [{"name": "nested"}]}
    )
    nested = {
        "name": "nested",
        "resolved_source": {"path": "unused", "sha256": "2" * 64},
    }
    lock["depends"][0]["dependencies"] = [nested]
    _write_document(tmp_path / "inspec.lock", lock)
    with pytest.raises(ValueError, match="Missing unpacked"):
        inspect_profile_bundle(tmp_path)
    nested_path = tmp_path / "vendor" / ("2" * 64)
    _write_document(nested_path / "inspec.yml", {"name": "nested-resource"})
    assert len(inspect_profile_bundle(tmp_path)[1]) == 2
    monkeypatch.setattr("kerkov.core.compliance.profile_bundle.MAX_DEPENDENCY_DEPTH", 1)
    with pytest.raises(ValueError, match="exceeds"):
        inspect_profile_bundle(tmp_path)
    monkeypatch.undo()
    _write_document(
        nested_path / "inspec.yml",
        {"name": "nested-resource", "depends": [{"name": "shared"}]},
    )
    nested["dependencies"] = [lock["depends"][0].copy()]
    _write_document(tmp_path / "inspec.lock", lock)
    with pytest.raises(ValueError, match="cycle"):
        inspect_profile_bundle(tmp_path)


def test_hidden_lockfile_dependencies_and_external_symlinks_are_rejected(tmp_path):
    child, lock = _create_mock_bundle(tmp_path / "project")
    _write_document(child / "inspec.lock", lock)
    with pytest.raises(ValueError, match="declared"):
        inspect_profile_bundle(tmp_path / "project")
    (child / "inspec.lock").unlink()
    outside = tmp_path / "external"
    child.rename(outside)
    try:
        child.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    with pytest.raises(ValueError, match="Missing unpacked"):
        inspect_profile_bundle(tmp_path / "project")


@pytest.mark.parametrize("ref", ["main", "v2.0", "a" * 39, None])
def test_git_locks_require_full_immutable_commit_ids(tmp_path, ref):
    _, lock = _create_mock_bundle(tmp_path)
    lock["depends"][0]["resolved_source"] = {
        "git": "https://example.invalid/repo",
        "ref": ref,
    }
    _write_document(tmp_path / "inspec.lock", lock)
    with pytest.raises(ValueError, match="full Git commit"):
        inspect_profile_bundle(tmp_path)


@pytest.mark.parametrize("git_lock", [False, True])
def test_upstream_style_resource_bundle_executes_offline_in_cinc(tmp_path, git_lock):
    image = os.environ.get("KERKOV_CINC_TEST_IMAGE")
    if not image or os.name == "nt":
        pytest.skip("set KERKOV_CINC_TEST_IMAGE to an installed local image")
    source = tmp_path / "project"
    child, lock = _create_mock_bundle(source)
    key = "1" * 64
    if git_lock:
        key = "2" * 40
        child.rename(source / "vendor" / key)
        lock["depends"][0]["resolved_source"] = {
            "git": "https://example.invalid/unavailable",
            "ref": key,
        }
        _write_document(source / "inspec.lock", lock)
    assert inspect_profile_bundle(source)[1][0]["cache_key"] == key
    snapshot = tmp_path / "snapshot"
    snapshot_profile(source, snapshot, tmp_path, set())
    (source / "vendor" / key / "libraries/shared.rb").unlink()
    output = tmp_path / "output"
    output.mkdir()
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,mode=1777",
            "--env",
            "HOME=/tmp",
            "--mount",
            f"type=bind,src={snapshot},dst=/profile,readonly",
            "--mount",
            f"type=bind,src={output},dst=/output",
            image,
            "exec",
            "/profile",
            "--vendor-cache=/tmp/kerkov-profile-cache",
            "--no-create-lockfile",
            "--no-auto-install-gems",
            "--reporter",
            "json:/output/result.json",
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = parse_report((output / "result.json").read_bytes())
    assert report["counts"]["passed"] == 1
    assert report["controls"][0]["control_id"] == "upstream-control"
