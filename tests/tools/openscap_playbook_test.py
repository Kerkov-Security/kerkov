"""Exercise the bundled playbook through real Ansible with a fixture scanner."""

import asyncio
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kerkov.core.automation.ansible import ansible_binary
from kerkov.core.compliance.review import build_report, review_control
from kerkov.core.compliance.workflows import WorkflowRequest, new_record
from kerkov.core.config_manager import ConfigManager
from kerkov.core.run_workspace import reset_current_workspace, set_current_workspace
from kerkov.tools.ansible_tool import AnsibleTool
from kerkov.tools.compliance_report_tool import ComplianceReportTool
from kerkov.tools.compliance_scan_tool import ComplianceScanTool

PLAYBOOK = (
    Path(__file__).parents[2]
    / "src/kerkov/config/ansible/playbooks/openscap_assess.yml"
)
FIXTURE = Path(__file__).parents[1] / "core/compliance/fixtures/results.xml"


def _create_mock_scanner(workspace: Path) -> Path:
    script = workspace / "fixture-oscap"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        f"calls = Path({str(workspace / 'calls.jsonl')!r})\n"
        "with calls.open('a') as handle:\n"
        "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('Fixture OpenSCAP 1.0')\n"
        "    sys.exit(0)\n"
        "if sys.argv[sys.argv.index('--profile') + 1].endswith('_broken'):\n"
        "    sys.exit(1)\n"
        f"with Path({str(workspace / 'scanned.jsonl')!r}).open('a') as handle:\n"
        "    handle.write(json.dumps(Path(sys.argv[-1]).read_text()) + '\\n')\n"
        f"raw = Path({str(FIXTURE)!r}).read_text()\n"
        "for option in ('--results', '--results-arf', '--report'):\n"
        "    output = Path(sys.argv[sys.argv.index(option) + 1])\n"
        "    if option == '--results-arf':\n"
        "        body = raw[raw.index('<Benchmark'):]\n"
        "        output.write_text('<asset-report-collection xmlns=\"http://scap.nist.gov/schema/asset-reporting-format/1.1\"><reports><report><content>' + body + '</content></report></reports></asset-report-collection>')\n"
        "    else:\n"
        "        output.write_text('<html>Fixture report</html>' if option == '--report' else raw)\n"
        "sys.exit(2)\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    return script


class OpenScapPlaybookTest(unittest.TestCase):
    def setUp(self):
        if ansible_binary() is None:
            self.skipTest("ansible-playbook is not installed")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        token = set_current_workspace(self.workspace)
        self.addCleanup(reset_current_workspace, token)
        self.scanner = _create_mock_scanner(self.workspace)
        content = self.workspace / "datastream.xml"
        content.write_text("fixture content")
        (self.workspace / "hosts.json").write_text(
            json.dumps(
                {
                    "all": {
                        "hosts": {
                            name: {
                                "ansible_connection": "local",
                                "ansible_python_interpreter": sys.executable,
                            }
                            for name in ("first", "second")
                        }
                    }
                }
            )
        )
        self.variables = {
            "openscap_content": str(content),
            "openscap_content_sha256": hashlib.sha256(content.read_bytes()).hexdigest(),
            "openscap_profile": "xccdf_example_profile_server",
            "openscap_binary": str(self.scanner),
            "openscap_expected_distribution": "FixtureLinux",
            "openscap_expected_version": "1",
            "ansible_facts": {
                "system": "Linux",
                "distribution": "FixtureLinux",
                "distribution_version": "1",
            },
        }

    def _run(self, **kwargs):
        (self.workspace / "vars.json").write_text(json.dumps(self.variables))
        return asyncio.run(
            AnsibleTool(execution_enabled=True).execute(
                {
                    "playbook": str(PLAYBOOK),
                    "inventory": "hosts.json",
                    "vars_files": ["vars.json"],
                    "check": False,
                    "timeout_seconds": 90,
                    **kwargs,
                }
            )
        )

    def test_completed_scans_with_failures_collect_isolated_evidence_for_two_hosts(
        self,
    ):
        run = self._run()
        self.assertTrue(run.success, run.error)
        directory = Path(run.metadata["artifact_directory"])
        for host in ("first", "second"):
            evidence = directory / "openscap" / host
            self.assertEqual(
                (evidence / "results.xml").read_bytes(), FIXTURE.read_bytes()
            )
            self.assertTrue((evidence / "report.html").exists())
            manifest = json.loads((evidence / "manifest.json").read_text())
            self.assertEqual(manifest["inventory_alias"], host)
            self.assertEqual(
                manifest["content_sha256"], self.variables["openscap_content_sha256"]
            )
            self.assertEqual(manifest["scanner_exit_code"], 2)
            if os.name != "nt":
                self.assertEqual(evidence.stat().st_mode & 0o777, 0o700)
            for name in ("results.xml", "results-arf.xml"):
                imported = asyncio.run(
                    ComplianceReportTool().execute(
                        {
                            "report": str(evidence / name),
                            "format": "openscap",
                            "expected_profile": "xccdf_example_profile_server",
                        }
                    )
                )
                self.assertTrue(imported.success, imported.error)
                self.assertEqual(imported.metadata["counts"]["fail"], 2)
        calls = [
            json.loads(line)
            for line in (self.workspace / "calls.jsonl").read_text().splitlines()
        ]
        scans = [args for args in calls if "eval" in args]
        self.assertEqual(len(scans), 2)
        for args in scans:
            self.assertNotIn("--remediate", args)
            self.assertNotIn("--fetch-remote-resources", args)
            self.assertFalse(Path(args[args.index("--results") + 1]).parent.exists())

    def test_check_mode_and_digest_mismatch_never_invoke_the_scanner(self):
        run = self._run(check=True)
        self.assertFalse(run.success)
        self.assertFalse((self.workspace / "calls.jsonl").exists())
        self.variables["openscap_content_sha256"] = "0" * 64
        run = self._run()
        self.assertFalse(run.success)
        self.assertFalse((self.workspace / "calls.jsonl").exists())

    def test_scanner_errors_are_not_completed_assessments(self):
        self.variables["openscap_profile"] = "xccdf_example_profile_broken"
        run = self._run()
        self.assertFalse(run.success)
        self.assertEqual(run.metadata["totals"]["failures"], 2)
        self.assertFalse(
            (Path(run.metadata["artifact_directory"]) / "openscap").exists()
        )

    def test_install_alias_in_check_mode_does_not_provision(self):
        self.variables.update(
            ansible_become=False,
            ansible_facts={
                "system": "Linux",
                "distribution": "Ubuntu",
                "distribution_version": "24.04",
            },
        )
        run = self._run(playbook="builtin:openscap_install", check=True, limit="first")
        self.assertTrue(run.success, run.error)
        self.assertEqual(run.metadata["totals"]["changed"], 0)
        self.assertGreaterEqual(run.metadata["totals"]["skipped"], 1)

    def test_scanner_reads_the_verified_copy_when_original_changes(self):
        source = Path(self.variables["openscap_content"])
        script = self.scanner.read_text()
        self.scanner.write_text(
            script.replace(
                "    print('Fixture OpenSCAP 1.0')",
                f"    Path({str(source)!r}).write_text('changed after checksum')\n"
                "    print('Fixture OpenSCAP 1.0')",
            )
        )
        run = self._run(limit="first")
        self.assertTrue(run.success, run.error)
        self.assertEqual(source.read_text(), "changed after checksum")
        self.assertEqual(
            json.loads((self.workspace / "scanned.jsonl").read_text()),
            "fixture content",
        )

    def test_compliance_scan_stages_local_content_and_reviews_evidence(self):
        key = self.workspace / "key"
        key.write_text("fixture key")
        key.chmod(0o600)
        known_hosts = self.workspace / "known_hosts"
        known_hosts.write_text("fixture trust")
        datastream = self.workspace / "datastream.xml"
        datastream.write_text(
            '<data-stream-collection xmlns="http://scap.nist.gov/schema/scap/source/1.2">'
            '<Profile xmlns="http://checklists.nist.gov/xccdf/1.2" '
            'id="xccdf_example_profile_server"/></data-stream-collection>'
        )
        scan = {
            "host": "192.0.2.1",
            "user": "audit",
            "profile": str(datastream),
            "private_key_file": str(key),
            "known_hosts_file": str(known_hosts),
            "openscap": {
                "distribution": "FixtureLinux",
                "version": "1",
                "profile_id": self.variables["openscap_profile"],
                "sha256": hashlib.sha256(datastream.read_bytes()).hexdigest(),
            },
        }
        execute = AnsibleTool.execute

        async def run_fixture(tool, arguments):
            inventory_path = Path(arguments["inventory"])
            inventory = json.loads(inventory_path.read_bytes())
            host = inventory["all"]["hosts"]["selected_asset"]
            self.assertEqual(host["ansible_host"], scan["host"])
            self.assertIn("StrictHostKeyChecking=yes", host["ansible_ssh_common_args"])
            host.update(
                ansible_connection="local", ansible_python_interpreter=sys.executable
            )
            inventory_path.write_text(json.dumps(inventory))
            vars_path = Path(arguments["vars_files"][0])
            variables = json.loads(vars_path.read_bytes())
            self.assertTrue(variables["openscap_controller_content"])
            variables.update(
                ansible_facts=self.variables["ansible_facts"],
                openscap_binary=str(self.scanner),
            )
            vars_path.write_text(json.dumps(variables))
            return await execute(tool, arguments)

        with patch.object(AnsibleTool, "execute", run_fixture):
            result = asyncio.run(
                ComplianceScanTool(
                    config=ConfigManager(EXECUTION_ENABLED=True)
                ).execute(scan)
            )
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.metadata["engine"], "openscap")
        self.assertEqual(result.metadata["evidence_status"], "scanned")
        self.assertEqual(result.metadata["profile_sha256"], scan["openscap"]["sha256"])
        self.assertEqual(result.metadata["xccdf_counts"]["fail"], 2)
        self.assertEqual(result.metadata["control_count"], 9)
        request = WorkflowRequest.model_validate(
            {"action": "assess", "resource_id": 1, "scan": scan}
        )
        outcome = json.loads(result.output)
        self.assertEqual(outcome["controls"][-1]["xccdf_result"], "notselected")
        record = new_record(request, "OpenSCAP fixture")
        record.update(status="attention", before=outcome)
        report = build_report(self.workspace, record)
        self.assertEqual(report["assessments"]["before"]["engine"], "openscap")
        control = next(
            item
            for item in report["controls"]
            if item["control_id"].endswith("_permissions")
        )
        evidence = review_control(
            self.workspace, record, control["profile"], control["control_id"]
        )
        self.assertEqual(evidence["before"]["test_count"], 2)
