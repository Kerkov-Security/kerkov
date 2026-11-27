"""Checks for the no-code profile store and deterministic recommendations."""

import pytest

from kerkov.core.compliance.custom_profiles import (
    Check,
    CustomProfile,
    CustomProfileRef,
    CustomProfileStore,
)
from kerkov.core.compliance.profile_bundle import inspect_profile_bundle
from kerkov.core.compliance.recommendation import recommend_profile


def test_saved_revisions_compile_to_an_assessment(tmp_path):
    store = CustomProfileStore(tmp_path)
    draft = CustomProfile(
        name="Web host",
        checks=[
            Check(kind="file_mode", target="/etc/passwd", expected="0644"),
            Check(kind="package_absent", target="telnet"),
        ],
    )
    entry = store.save(draft, None)
    ref = CustomProfileRef(id=entry["id"], revision=entry["revision"])
    assert store.list() == [entry]
    output = tmp_path / "profile"
    output.mkdir()
    store.get(ref).render(ref.id, output)
    metadata, dependencies = inspect_profile_bundle(output)
    assert metadata["title"] == draft.name
    assert dependencies == []
    checks = (output / "controls/checks.rb").read_text()
    assert "its('mode') { should cmp '0644' }" in checks
    assert "describe package('telnet')" in checks
    revised = store.save(draft.model_copy(update={"name": "Updated checks"}), ref)
    assert revised["id"] == ref.id
    assert revised["revision"] != ref.revision
    with pytest.raises(ValueError, match="profile changed"):
        store.get(ref)
    with pytest.raises(ValueError, match="profile changed"):
        store.save(draft, ref)


@pytest.mark.parametrize("target", ["/tmp/#{code}", "/tmp/a'\\nend", "relative-file"])
def test_rejects_targets_outside_supported_check_syntax(target):
    with pytest.raises(ValueError, match="absolute Linux file path"):
        Check(kind="file_exists", target=target)


@pytest.mark.parametrize(
    "distro,version,goal,expected",
    [
        ("ubuntu", "24.04", "stig", "mitre-ubuntu-2404-stig"),
        ("rhel", "9.6", "stig", "mitre-rhel-9-stig"),
        ("rhel", "10.1", "pci", "pci-dss-rhel10-"),
        ("debian", "13", "cis", "devsec-cis-linux"),
        ("fedora", "43", "hardening", "devsec-linux"),
    ],
)
def test_auto_matches_verified_distro_release_and_goal(distro, version, goal, expected):
    result = recommend_profile(
        f'KERKOV_OS_RELEASE=Linux\nID={distro}\nVERSION_ID="{version}"', goal
    )
    assert result["id"].startswith(expected)
    assert version in result["reason"]


def test_auto_requires_a_match_and_never_uses_other_framework_as_fallback():
    with pytest.raises(ValueError, match="No PCI DSS"):
        recommend_profile("KERKOV_OS_RELEASE=Linux\nID=ubuntu\nVERSION_ID=24.04", "pci")
    with pytest.raises(ValueError, match="confirm a Linux host"):
        recommend_profile(
            "KERKOV_OS_RELEASE=Darwin\nID=ubuntu\nVERSION_ID=24.04", "stig"
        )
