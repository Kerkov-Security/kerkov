"""SSH trust-file paths survive both Ansible and OpenSSH argument parsing."""

import shlex
import shutil
import subprocess

import pytest

from kerkov.core.automation.ansible import ssh_inventory_host


@pytest.mark.parametrize(
    "path", ['/tmp/quoted"path', "/tmp/%h", "/tmp/${HOME}", "/tmp/line\nfeed"]
)
def test_expanding_or_ambiguous_trust_paths_are_rejected(path):
    with pytest.raises(ValueError, match="known-hosts path"):
        ssh_inventory_host("example.invalid", "audit", 22, "/tmp/key", path)


@pytest.mark.skipif(shutil.which("ssh") is None, reason="OpenSSH is not installed")
@pytest.mark.parametrize("name", ["known_hosts", "trust café's file", "trust\\file"])
def test_literal_trust_path_reaches_openssh_as_one_file(tmp_path, name):
    trust = str(tmp_path / name)
    host = ssh_inventory_host("example.invalid", "audit", 22, "/tmp/key", trust)
    arguments = shlex.split(host["ansible_ssh_common_args"])
    result = subprocess.run(
        ["ssh", "-G", *arguments, "example.invalid"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert f"userknownhostsfile {trust}" in result.stdout.splitlines()
    option = arguments[arguments.index("-o", arguments.index("UpdateHostKeys=no")) + 1]
    assert shlex.split(option.split("=", 1)[1]) == [trust]
