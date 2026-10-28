"""Built-in baseline coverage, packaged resources and executable playbook syntax."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from kerkov.core.automation.ansible import command_environment
from kerkov.core.compliance.baselines import builtin_baselines
from kerkov.core.compliance.cinc_report import parse_report


def test_builtin_baselines_have_paired_profiles_and_existing_only_remediation():
    catalog = builtin_baselines()
    assert len({item["id"] for item in catalog}) == len(catalog) >= 2
    for item in catalog:
        profile = Path(item["profile"])
        metadata = yaml.safe_load((profile / "inspec.yml").read_text())
        assert metadata["name"] == item["profile_name"]
        assert metadata["version"] == "1.0.0"
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
printf '#!/bin/sh\nprintf "permitemptypasswords no\\nignorerhosts yes\\nx11forwarding no\\nmaxauthtries 4\\nlogingracetime 60\\n"\n' > /usr/sbin/sshd
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
