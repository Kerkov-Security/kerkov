"""Built-in baseline coverage, packaged resources and executable playbook syntax."""

import copy
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from kerkov.core.automation.ansible import command_environment, load_summary
from kerkov.core.compliance.baselines import builtin_baselines
from kerkov.core.compliance.cinc_report import parse_report


def test_builtin_baselines_have_paired_profiles_and_existing_only_remediation():
    catalog = builtin_baselines()
    assert len({item["id"] for item in catalog}) == len(catalog) >= 2
    for item in catalog:
        profile = Path(item["profile"])
        metadata = yaml.safe_load((profile / "inspec.yml").read_text())
        assert metadata["name"] == item["profile_name"]
        assert metadata["version"] == item["version"]
        assert len(set(item["controls"])) == len(item["controls"]) > 0
        play = yaml.safe_load(Path(item["playbook"]).read_text())[0]
        assert play["hosts"] == "all" and play["become"] is True
        guard = play["tasks"][0]["ansible.builtin.assert"]
        assert "['Debian', 'Ubuntu']" in guard["that"][0]
        for task in [*play["tasks"], *play.get("handlers", [])]:
            modules = {key for key in task if key.startswith("ansible.builtin.")}
            assert modules <= {
                "ansible.builtin.assert",
                "ansible.builtin.stat",
                "ansible.builtin.file",
                "ansible.builtin.service_facts",
                "ansible.builtin.blockinfile",
                "ansible.builtin.service",
                "ansible.builtin.command",
            }


@pytest.mark.parametrize("baseline", builtin_baselines(), ids=lambda item: item["id"])
def test_builtin_playbook_passes_ansible_syntax_check(baseline, tmp_path):
    binary = shutil.which("ansible-playbook")
    if binary is None:
        pytest.skip("optional Ansible installation is unavailable")
    result = subprocess.run(
        [binary, "--syntax-check", "-i", "localhost,", baseline["playbook"]],
        cwd=tmp_path,
        env=command_environment(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("baseline", builtin_baselines(), ids=lambda item: item["id"])
def test_builtin_profile_controls_execute_in_cinc(baseline, tmp_path):
    image = os.environ.get("KERKOV_CINC_TEST_IMAGE")
    if not image:
        pytest.skip("set KERKOV_CINC_TEST_IMAGE to an installed local image")
    script = """
set -eu
mkdir -p /usr/sbin
printf '#!/bin/sh\nprintf "permitemptypasswords yes\\nignorerhosts no\\nx11forwarding yes\\nmaxauthtries 6\\nlogingracetime 120\\n"\n' > /usr/sbin/sshd
chmod 755 /usr/sbin/sshd
chmod 666 /etc/passwd /etc/group /etc/shadow /etc/gshadow
if /opt/cinc-auditor/bin/cinc-auditor exec /baseline --no-create-lockfile --reporter json:/output/before.json; then
  exit 1
else
  test "$?" -eq 100
fi
printf '#!/bin/sh\nprintf "permitemptypasswords no\\nignorerhosts yes\\nx11forwarding no\\nmaxauthtries 2\\nlogingracetime 20\\n"\n' > /usr/sbin/sshd
chmod 755 /usr/sbin/sshd
chown root:root /etc/passwd /etc/group
chown root:shadow /etc/shadow /etc/gshadow
chmod 644 /etc/passwd /etc/group
chmod 640 /etc/shadow /etc/gshadow
exec /opt/cinc-auditor/bin/cinc-auditor exec /baseline --no-create-lockfile --reporter json:/output/report.json
"""
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--network=none",
            "--mount",
            f"type=bind,src={baseline['profile']},dst=/baseline,readonly",
            "--mount",
            f"type=bind,src={tmp_path},dst=/output",
            "--entrypoint=/bin/sh",
            image,
            "-c",
            script,
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    before = parse_report(
        (tmp_path / "before.json").read_bytes(),
        expected_profile=baseline["profile_name"],
    )
    assert before["counts"]["failed"] == len(baseline["controls"])
    report = parse_report(
        (tmp_path / "report.json").read_bytes(),
        expected_profile=baseline["profile_name"],
    )
    assert {row["control_id"] for row in report["controls"]} == set(
        baseline["controls"]
    )
    assert report["counts"]["passed"] == len(baseline["controls"])
    assert report["has_inconclusive_results"] is False


@pytest.mark.parametrize(
    "auth,grace,expected_auth,expected_grace",
    [(2, 20, 2, 20), (6, 120, 4, 60), (4, 0, 4, 60)],
)
def test_ssh_remediation_preserves_stricter_limits(
    tmp_path, auth, grace, expected_auth, expected_grace
):
    """Exercise the shipped template without accessing the controller's SSH service."""
    binary = shutil.which("ansible-playbook")
    if binary is None or sys.platform == "win32":
        pytest.skip("optional POSIX Ansible installation is unavailable")
    baseline = next(
        item for item in builtin_baselines() if item["id"] == "ssh-essentials"
    )
    playbook = yaml.safe_load(Path(baseline["playbook"]).read_text())[0]
    task = copy.deepcopy(
        next(
            task for task in playbook["tasks"] if "ansible.builtin.blockinfile" in task
        )
    )
    configuration = tmp_path / "sshd_config"
    original = f"MaxAuthTries {auth}\nLoginGraceTime {grace}\n"
    configuration.write_text(original)
    effective = tmp_path / "effective.txt"
    effective.write_text(f"maxauthtries {auth}\nlogingracetime {grace}\n")
    task["ansible.builtin.blockinfile"]["path"] = str(configuration)
    task["ansible.builtin.blockinfile"]["validate"] = "/usr/bin/true %s"
    task.pop("notify")
    fixture = tmp_path / "fixture.yml"
    fixture.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "all",
                    "gather_facts": False,
                    "tasks": [
                        {
                            "name": "Read fixture effective limits",
                            "ansible.builtin.command": {
                                "argv": ["cat", str(effective)]
                            },
                            "register": "ssh_effective",
                            "changed_when": False,
                            "check_mode": False,
                        },
                        task,
                    ],
                }
            ]
        )
    )
    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        '{"all":{"hosts":{"localhost":{"ansible_connection":"local"}}}}'
    )
    args = [binary, "-i", str(inventory), str(fixture)]
    environment = command_environment(tmp_path)
    for options in (["--check"], [], []):
        result = subprocess.run(
            [*args, *options],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        if options:
            assert configuration.read_text() == original
    content = configuration.read_text()
    managed = content.split("# END KERKOV SSH ESSENTIALS", 1)[0]
    assert f"MaxAuthTries {expected_auth}\n" in managed
    assert f"LoginGraceTime {expected_grace}\n" in managed
    assert load_summary(tmp_path, 1)["totals"]["changed"] == 0
