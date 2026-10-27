"""Cinc runtime ownership, bounded evidence, approvals and cancellation."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kerkov.core.compliance.cinc_runtime import DockerCincRuntime, prepare_scan
from kerkov.core.config_manager import ConfigManager
from kerkov.core.docker_command import CommandResult
from kerkov.core.process.command_runner import CommandResult as ScanResult
from kerkov.core.run_workspace import reset_current_workspace, set_current_workspace
from kerkov.tools.compliance_scan_tool import ComplianceScanTool

FIXTURE = Path(__file__).parents[1] / "core/compliance/fixtures/cinc.json"
IMAGE_ID = "sha256:" + "1" * 64


class _FakeDockerRunner:
    def __init__(self):
        self.calls = []
        self.owner = ""
        self.exit_code = 100
        self.unmanaged = False
        self.missing_image = False
        self.preflight_error = False
        self.remove_error = False
        self.started_create = None
        self.finish_create = None
        self.pull_wait = None

    async def run(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        if args[1] == "info":
            return (
                CommandResult(1, stderr="Cannot connect")
                if self.preflight_error
                else CommandResult(0, stdout="20.10")
            )
        if args[1:3] == ["image", "inspect"]:
            return (
                CommandResult(1)
                if self.missing_image
                else CommandResult(0, stdout=IMAGE_ID)
            )
        if args[1] == "pull":
            if self.pull_wait is not None:
                await self.pull_wait.wait()
            self.missing_image = False
            return CommandResult(0)
        if args[1] == "create":
            self.owner = next(
                value.split("=", 1)[1]
                for value in args
                if value.startswith("org.kerkov.execution=")
            )
            if self.started_create:
                self.started_create.set()
                await self.finish_create.wait()
            return CommandResult(0, stdout="container-id")
        if args[1] == "inspect":
            if ".State.Status" in args[3]:
                return CommandResult(0, stdout=f"exited|{self.exit_code}")
            return CommandResult(
                0,
                stdout=(
                    "foreign|owner" if self.unmanaged else "cinc-auditor|" + self.owner
                ),
            )
        if args[1] == "rm":
            return CommandResult(1 if self.remove_error else 0)
        raise AssertionError(args)


class _FakeScanRunner:
    def __init__(self):
        self.calls = []
        self.result = ScanResult(0)
        self.write_report = True
        self.started = None
        self.wait = None

    async def run(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        kwargs["log_path"].write_text("raw-secret-scanner-output")
        if self.started:
            self.started.set()
            await self.wait.wait()
        if self.write_report:
            (kwargs["cwd"] / "output/source.json").write_bytes(FIXTURE.read_bytes())
        return self.result


class ComplianceScanToolTest(unittest.TestCase):
    def setUp(self):
        if os.name == "nt":
            self.skipTest("Docker controller uses Linux/macOS/WSL paths")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        token = set_current_workspace(self.workspace)
        self.addCleanup(reset_current_workspace, token)
        profile = self.workspace / "baseline"
        (profile / "controls").mkdir(parents=True)
        (profile / "inspec.yml").write_text("name: fixture\nversion: '1.0'\n")
        (profile / "controls/check.rb").write_text(
            "describe 1 do; it { should eq 1 }; end"
        )
        (self.workspace / "key").write_text("raw-secret-key")
        (self.workspace / "key").chmod(0o600)
        (self.workspace / "known_hosts").write_text("trusted host entries")
        self.params = {
            "host": "192.0.2.10",
            "user": "audit",
            "profile": "baseline",
            "private_key_file": "key",
            "known_hosts_file": "known_hosts",
        }
        self.docker = _FakeDockerRunner()
        self.scanner = _FakeScanRunner()
        self.runtime = DockerCincRuntime(
            image="cincproject/auditor:7", runner=self.docker, scan_runner=self.scanner
        )
        self.tool = ComplianceScanTool(
            config=ConfigManager(EXECUTION_ENABLED=True), runtime=self.runtime
        )

    def test_completed_scan_preserves_failed_skipped_and_full_private_evidence(self):
        result = asyncio.run(self.tool.execute({**self.params, "max_controls": 1}))
        self.assertTrue(result.success, result.error)
        summary = json.loads(result.output)
        self.assertEqual(summary["counts"]["failed"], 1)
        self.assertEqual(len(summary["controls"]), 1)
        self.assertTrue(summary["controls_truncated"])
        normalized = json.loads(Path(summary["normalized_artifact"]).read_text())
        self.assertEqual(len(normalized["controls"]), 6)
        self.assertEqual(
            Path(summary["report_artifact"]).read_bytes(), FIXTURE.read_bytes()
        )
        self.assertNotIn("raw-secret", result.output + json.dumps(result.metadata))
        self.assertEqual(result.metadata["image_id"], IMAGE_ID)
        self.assertEqual(self.docker.calls[-1][0][1:3], ["rm", "--force"])
        self.assertEqual(Path(summary["report_artifact"]).parent.name, "output")
        self.assertFalse((Path(summary["artifact_directory"]) / "source.json").exists())
        manifest = json.loads(Path(summary["manifest_artifact"]).read_text())
        self.assertEqual(manifest["status"], "completed")

    def test_mounts_are_scoped_and_host_verification_is_explicit(self):
        result = asyncio.run(self.tool.execute(self.params))
        self.assertTrue(result.success, result.error)
        args = next(args for args, _ in self.docker.calls if args[1] == "create")
        self.assertIn(IMAGE_ID, args)
        self.assertIn("--read-only", args)
        self.assertIn("--cap-drop=ALL", args)
        self.assertNotIn("--privileged", args)
        self.assertNotIn("--publish", args)
        self.assertNotIn("docker.sock", " ".join(args))
        self.assertIn("target=/etc/ssh/ssh_known_hosts,readonly", " ".join(args))
        config = json.loads(
            (
                Path(result.metadata["artifact_directory"]) / "config/connection.json"
            ).read_text()
        )
        self.assertTrue(config["verify_host_key"])
        self.assertTrue(config["non_interactive"])
        self.assertEqual(config["ssh_config_file"], [])
        self.assertFalse(config["forward_agent"])
        self.assertEqual(config["auth_methods"], ["publickey"])

    def test_missing_image_is_pulled_once_and_execution_uses_immutable_id(self):
        self.docker.missing_image = True
        result = asyncio.run(self.tool.execute(self.params))
        self.assertTrue(result.success, result.error)
        self.assertTrue(result.metadata["image_pulled"])
        self.assertEqual(sum(args[1] == "pull" for args, _ in self.docker.calls), 1)

    def test_cleanup_and_image_metadata_survive_an_execution_error(self):
        for foreign in (False, True):
            with self.subTest(foreign=foreign):
                self.docker.missing_image = True
                self.docker.remove_error = True
                self.docker.unmanaged = foreign
                self.scanner.result = ScanResult(
                    None, timed_out=True, error="scanner deadline reached"
                )
                result = asyncio.run(self.tool.execute(self.params))
                self.assertFalse(result.success)
                self.assertTrue(result.metadata["image_pulled"])
                self.assertEqual(result.metadata["image_id"], IMAGE_ID)
                self.assertTrue(result.metadata["cleanup_error"])
                self.assertIn("scanner deadline reached", result.error)
                manifest = json.loads(
                    Path(result.metadata["manifest_artifact"]).read_text()
                )
                self.assertEqual(manifest, result.metadata)

    def test_pull_deadline_names_the_phase_and_uses_remaining_budget(self):
        async def scenario():
            self.docker.pull_wait = asyncio.Event()
            self.docker.missing_image = True
            prepared = await asyncio.to_thread(
                self._prepare,
            )
            metadata = {}
            with self.assertRaisesRegex(RuntimeError, "image pull timed out") as error:
                await self.runtime.run(prepared, 0.05, metadata=metadata)
            self.assertNotIn("scanner.log", str(error.exception))
            self.assertIn("did not start", str(error.exception))
            return metadata

        metadata = asyncio.run(scenario())
        pull = next(kwargs for args, kwargs in self.docker.calls if args[1] == "pull")
        self.assertGreater(pull["timeout_seconds"], 0)
        self.assertLessEqual(pull["timeout_seconds"], 0.05)
        self.assertFalse(metadata["image_pulled"])
        self.assertFalse(any(args[1] == "create" for args, _ in self.docker.calls))

    def _prepare(self):
        return prepare_scan(
            self.workspace,
            profile=self.params["profile"],
            private_key_file=self.params["private_key_file"],
            known_hosts_file=self.params["known_hosts_file"],
            inputs_file=None,
            host=self.params["host"],
            user=self.params["user"],
            port=22,
            sudo=False,
        )

    def test_root_profile_excludes_generated_artifacts_and_explicit_ssh_files(self):
        (self.workspace / "inspec.yml").write_text("name: fixture\n")
        artifact = self.workspace / "artifacts/old-run/secret.json"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("previous raw evidence")
        self.params["profile"] = "."
        first = asyncio.run(self.tool.execute(self.params))
        second = asyncio.run(self.tool.execute(self.params))
        self.assertTrue(first.success, first.error)
        self.assertTrue(second.success, second.error)
        self.assertEqual(
            first.metadata["profile_sha256"], second.metadata["profile_sha256"]
        )
        snapshot = Path(second.metadata["artifact_directory"]) / "profile"
        self.assertTrue((snapshot / "baseline/controls/check.rb").is_file())
        for excluded in ("artifacts", "key", "known_hosts"):
            self.assertFalse((snapshot / excluded).exists())

    def test_snapshot_rejections_remove_partial_artifacts(self):
        for limit, value in (("MAX_PROFILE_FILES", 1), ("MAX_PROFILE_BYTES", 1)):
            with (
                self.subTest(limit=limit),
                patch("kerkov.core.compliance.cinc_runtime." + limit, value),
            ):
                result = asyncio.run(self.tool.execute(self.params))
                self.assertFalse(result.success)
                self.assertEqual(
                    list((self.workspace / "artifacts/compliance").iterdir()), []
                )
        (self.workspace / "baseline/z-link").symlink_to(self.workspace / "key")
        result = asyncio.run(self.tool.execute(self.params))
        self.assertFalse(result.success)
        self.assertEqual(list((self.workspace / "artifacts/compliance").iterdir()), [])
        self.assertFalse(self.docker.calls)

    def test_private_key_requires_current_user_ownership(self):
        with patch(
            "kerkov.core.compliance.cinc_runtime.os.getuid",
            return_value=os.getuid() + 1,
        ):
            result = asyncio.run(self.tool.execute(self.params))
        self.assertFalse(result.success)
        self.assertIn("owned by the current user", result.error)
        self.assertFalse((self.workspace / "artifacts").exists())

    def test_malformed_scanner_evidence_survives_failed_parsing(self):
        with patch(
            "kerkov.core.compliance.cinc_report.parse_report",
            side_effect=ValueError("Invalid Cinc/InSpec JSON report"),
        ):
            result = asyncio.run(self.tool.execute(self.params))
        self.assertFalse(result.success)
        evidence = Path(result.metadata["report_artifact"])
        self.assertEqual(evidence.read_bytes(), FIXTURE.read_bytes())
        self.assertEqual(evidence.parent.name, "output")
        self.assertFalse((evidence.parent.parent / "source.json").exists())
        self.assertNotIn("normalized_artifact", result.metadata)

    def test_execution_gate_prevents_docker_and_artifact_side_effects(self):
        tool = ComplianceScanTool(
            config=ConfigManager(EXECUTION_ENABLED=False), runtime=self.runtime
        )
        result = asyncio.run(tool.execute(self.params))
        self.assertFalse(result.success)
        self.assertFalse(self.docker.calls)
        self.assertFalse((self.workspace / "artifacts").exists())

    def test_invalid_requests_never_reach_docker(self):
        for changes in (
            {"host": "ssh://user:secret@host"},
            {"host": "-flag"},
            {"port": 0},
            {"private_key_file": "../key"},
            {"profile": "../baseline"},
            {"password": "secret"},
            {"profile": "https://example.com/profile"},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(
                    asyncio.run(self.tool.execute({**self.params, **changes})).success
                )
        self.assertFalse(self.docker.calls)

    def test_missing_report_and_invalid_exit_are_execution_failures(self):
        self.scanner.write_report = False
        self.assertFalse(asyncio.run(self.tool.execute(self.params)).success)
        self.scanner.write_report = True
        self.docker.exit_code = 1
        self.assertFalse(asyncio.run(self.tool.execute(self.params)).success)
        self.assertEqual(sum(args[1] == "rm" for args, _ in self.docker.calls), 2)

    def test_timeout_removes_the_owned_container_and_reports_failure(self):
        self.scanner.result = ScanResult(None, timed_out=True)
        result = asyncio.run(self.tool.execute(self.params))
        self.assertFalse(result.success)
        self.assertIn("did not finish", result.error)
        self.assertEqual(self.docker.calls[-1][0][1], "rm")

    def test_snapshot_is_stable_and_credential_files_are_not_copied(self):
        first = asyncio.run(self.tool.execute(self.params))
        second = asyncio.run(self.tool.execute(self.params))
        self.assertEqual(
            first.metadata["profile_sha256"], second.metadata["profile_sha256"]
        )
        self.assertNotEqual(
            first.metadata["container_name"], second.metadata["container_name"]
        )
        directory = Path(first.metadata["artifact_directory"])
        self.assertNotIn(
            "raw-secret-key",
            "".join(
                path.read_text() for path in directory.rglob("*") if path.is_file()
            ),
        )
        (self.workspace / "baseline/controls/check.rb").write_text("changed profile")
        third = asyncio.run(self.tool.execute(self.params))
        self.assertNotEqual(
            first.metadata["profile_sha256"], third.metadata["profile_sha256"]
        )
        self.assertNotEqual(
            (directory / "profile/controls/check.rb").read_text(), "changed profile"
        )

    def test_broad_key_permissions_and_mount_path_injection_are_rejected(self):
        (self.workspace / "key").chmod(0o644)
        self.assertFalse(asyncio.run(self.tool.execute(self.params)).success)
        (self.workspace / "key").chmod(0o600)
        (self.workspace / "bad,key").write_text("fixture")
        (self.workspace / "bad,key").chmod(0o600)
        self.assertFalse(
            asyncio.run(
                self.tool.execute({**self.params, "private_key_file": "bad,key"})
            ).success
        )
        self.assertFalse(self.docker.calls)

    def test_preflight_errors_are_actionable_and_do_not_create_a_container(self):
        self.docker.preflight_error = True
        result = asyncio.run(self.tool.execute(self.params))
        self.assertFalse(result.success)
        self.assertIn("Docker", result.error)
        self.assertFalse(any(args[1] == "create" for args, _ in self.docker.calls))

    def test_cleanup_never_removes_a_container_with_foreign_labels(self):
        self.docker.unmanaged = True
        result = asyncio.run(self.tool.execute(self.params))
        self.assertFalse(result.success)
        self.assertIn("ownership labels", result.metadata["cleanup_error"])
        self.assertFalse(any(args[1] == "rm" for args, _ in self.docker.calls))

    def test_cleanup_failure_is_visible(self):
        self.docker.remove_error = True
        result = asyncio.run(self.tool.execute(self.params))
        self.assertFalse(result.success)
        self.assertIn("Could not remove", result.metadata["cleanup_error"])

    def test_mutable_executable_profile_grants_cannot_be_saved(self):
        self.assertFalse(self.tool.approval_scope(self.params).saveable)

    def test_profile_snapshot_rejects_dependencies_and_symlinks(self):
        metadata = self.workspace / "baseline/inspec.yml"
        metadata.write_text(
            "name: fixture\ndepends: [{name: remote, url: 'https://example.com'}]\n"
        )
        self.assertFalse(asyncio.run(self.tool.execute(self.params)).success)
        metadata.write_text("name: fixture\n")
        (self.workspace / "baseline/key-link").symlink_to(self.workspace / "key")
        self.assertFalse(asyncio.run(self.tool.execute(self.params)).success)
        self.assertFalse(self.docker.calls)

    def test_cancellation_during_scan_removes_only_the_owned_container(self):
        async def scenario():
            self.scanner.started, self.scanner.wait = asyncio.Event(), asyncio.Event()
            task = asyncio.create_task(self.tool.execute(self.params))
            await self.scanner.started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        self.assertEqual(self.docker.calls[-1][0][1], "rm")

    def test_cancellation_during_create_waits_for_late_creation_then_cleans_it(self):
        async def scenario():
            self.docker.started_create, self.docker.finish_create = (
                asyncio.Event(),
                asyncio.Event(),
            )
            task = asyncio.create_task(self.tool.execute(self.params))
            await self.docker.started_create.wait()
            task.cancel()
            await asyncio.sleep(0)
            self.docker.finish_create.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        self.assertEqual(self.docker.calls[-1][0][1], "rm")
        self.assertFalse(self.scanner.calls)
