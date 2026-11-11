"""Opt-in SSH bootstrap against an owned disposable target without Python."""

import asyncio
import json
import os
import shutil
import subprocess
import uuid

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from kerkov.core.run_workspace import reset_current_workspace, set_current_workspace
from kerkov.tools.ansible_tool import AnsibleTool

IMAGE = os.environ.get("KERKOV_PYTHON_BOOTSTRAP_TEST_IMAGE")


def _run(args, *, timeout=60):
    return subprocess.run(
        args, check=True, capture_output=True, text=True, timeout=timeout
    ).stdout.strip()


@pytest.mark.integration
@pytest.mark.skipif(
    not IMAGE or os.name == "nt",
    reason="set KERKOV_PYTHON_BOOTSTRAP_TEST_IMAGE to a local Debian/Ubuntu/Alpine image",
)
def test_bootstrap_installs_before_facts_and_never_installs_in_preview(tmp_path):
    binary = shutil.which("ansible-playbook")
    if not binary or not shutil.which("docker"):
        pytest.skip("Docker and Ansible are required")
    _run(["docker", "image", "inspect", IMAGE])
    owner = uuid.uuid4().hex
    name = "kerkov-python-bootstrap-" + owner
    label = "org.kerkov.python-bootstrap-test"
    try:
        _run(
            [
                "docker",
                "run",
                "--detach",
                "--pull=never",
                "--name",
                name,
                "--label",
                f"{label}={owner}",
                "--publish",
                "127.0.0.1::22",
                IMAGE,
                "sleep",
                "600",
            ]
        )
        _run(
            [
                "docker",
                "exec",
                name,
                "sh",
                "-c",
                '. /etc/os-release; if [ "$ID" = alpine ]; then '
                "apk add --no-cache openssh; else apt-get update && "
                "DEBIAN_FRONTEND=noninteractive apt-get install "
                "-y --no-install-recommends openssh-server; fi; ssh-keygen -A",
            ],
            timeout=180,
        )
        _run(["docker", "exec", name, "sh", "-c", "! command -v python3"])
        key = ed25519.Ed25519PrivateKey.generate()
        private_key = tmp_path / "key"
        private_key.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.OpenSSH,
                serialization.NoEncryption(),
            )
        )
        private_key.chmod(0o600)
        public = (
            key.public_key()
            .public_bytes(
                serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
            )
            .decode()
        )
        _run(
            [
                "docker",
                "exec",
                name,
                "sh",
                "-c",
                "mkdir -p /run/sshd /root/.ssh && chmod 700 /root/.ssh && "
                f"printf '%s\\n' '{public}' > /root/.ssh/authorized_keys && "
                "chmod 600 /root/.ssh/authorized_keys && passwd -d root && "
                "/usr/sbin/sshd -o PasswordAuthentication=no -o PermitRootLogin=prohibit-password",
            ]
        )
        ports = json.loads(
            _run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{json .NetworkSettings.Ports}}",
                    name,
                ]
            )
        )
        port = int(ports["22/tcp"][0]["HostPort"])
        host_key = _run(
            ["docker", "exec", name, "cat", "/etc/ssh/ssh_host_ed25519_key.pub"]
        )
        known_hosts = tmp_path / "known_hosts"
        known_hosts.write_text(f"[127.0.0.1]:{port} {host_key}\n")
        inventory = tmp_path / "inventory.json"
        inventory.write_text(
            json.dumps(
                {
                    "all": {
                        "hosts": {
                            "selected": {
                                "ansible_host": "127.0.0.1",
                                "ansible_port": port,
                                "ansible_user": "root",
                                "ansible_private_key_file": str(private_key),
                                "ansible_ssh_common_args": f"-F /dev/null -o StrictHostKeyChecking=yes -o UserKnownHostsFile={known_hosts}",
                            },
                            "excluded": {"ansible_host": "192.0.2.99"},
                        }
                    }
                }
            )
        )
        playbook = tmp_path / "verify.yml"
        playbook.write_text(
            "- hosts: all\n  gather_facts: true\n  tasks:\n"
            "    - name: Verify the main playbook can use Python\n"
            "      ansible.builtin.copy:\n"
            "        dest: /tmp/kerkov-python-main-playbook\n"
            "        content: ready\n"
            "        mode: '0600'\n"
            "      tags: [selected]\n"
        )
        params = {
            "playbook": str(playbook),
            "inventory": str(inventory),
            "limit": "selected",
            "tags": ["selected"],
            "bootstrap_python": True,
            "timeout_seconds": 180,
        }
        token = set_current_workspace(tmp_path)
        try:
            tool = AnsibleTool(execution_enabled=True, binary=binary)
            _run(
                [
                    "docker",
                    "exec",
                    name,
                    "sh",
                    "-c",
                    "printf '#!/bin/sh\nexit 1\n' > /tmp/incompatible-python && chmod 755 /tmp/incompatible-python",
                ]
            )
            data = json.loads(inventory.read_text())
            for interpreter in ("/missing/custom/python", "/tmp/incompatible-python"):
                data["all"]["hosts"]["selected"][
                    "ansible_python_interpreter"
                ] = interpreter
                inventory.write_text(json.dumps(data))
                refused = asyncio.run(tool.execute({**params, "check": False}))
                assert not refused.success
                _run(
                    [
                        "docker",
                        "exec",
                        name,
                        "sh",
                        "-c",
                        "! command -v python3 && test ! -e /tmp/kerkov-python-main-playbook",
                    ]
                )
            del data["all"]["hosts"]["selected"]["ansible_python_interpreter"]
            inventory.write_text(json.dumps(data))
            preview = asyncio.run(tool.execute({**params, "check": True}))
            assert not preview.success
            assert "Report missing Python in preview" in preview.output
            _run(
                [
                    "docker",
                    "exec",
                    name,
                    "sh",
                    "-c",
                    "! command -v python3 && test ! -e /tmp/kerkov-python-main-playbook",
                ]
            )
            applied = asyncio.run(tool.execute({**params, "check": False}))
            assert applied.success, applied.output + str(applied.error)
            assert applied.metadata["host_count"] == 1
            _run(
                [
                    "docker",
                    "exec",
                    name,
                    "python3",
                    "-c",
                    "import sys; assert sys.version_info >= (3, 9)",
                ]
            )
            assert (
                _run(
                    ["docker", "exec", name, "cat", "/tmp/kerkov-python-main-playbook"]
                )
                == "ready"
            )
            repeated = asyncio.run(tool.execute({**params, "check": False}))
            assert repeated.success, repeated.error
            assert repeated.metadata["totals"]["changed"] == 0
        finally:
            reset_current_workspace(token)
    finally:
        inspection = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                f'{{{{ index .Config.Labels "{label}" }}}}',
                name,
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if inspection.returncode == 0 and inspection.stdout.strip() == owner:
            subprocess.run(
                ["docker", "rm", "--force", name], capture_output=True, timeout=30
            )
