"""Local evidence imports preserve artifacts and report control failures as data."""

import asyncio
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kerkov.core.run_workspace import reset_current_workspace, set_current_workspace
from kerkov.tools.compliance_report_tool import ComplianceReportTool

FIXTURE = Path(__file__).parents[1] / "core" / "compliance" / "fixtures" / "results.xml"


class OpenScapReportToolTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.token = set_current_workspace(self.workspace)
        self.addCleanup(reset_current_workspace, self.token)
        (self.workspace / "evidence.xml").write_bytes(FIXTURE.read_bytes())
        self.tool = ComplianceReportTool()

    async def _execute(self, params):
        return await self.tool.execute({"format": "openscap", **params})

    def test_import_with_control_failures_succeeds_and_retains_complete_evidence(self):
        result = asyncio.run(
            self._execute({"report": "evidence.xml", "max_controls": 1})
        )
        self.assertTrue(result.success, result.error)
        summary = json.loads(result.output)
        self.assertEqual(summary["counts"]["fail"], 2)
        self.assertEqual(len(summary["rules"]), 1)
        self.assertEqual(summary["rules"][0]["result"], "error")
        self.assertTrue(summary["rules_truncated"])
        normalized = json.loads(Path(summary["normalized_artifact"]).read_text())
        self.assertEqual(len(normalized["rules"]), 10)
        raw = Path(summary["report_artifact"]).read_bytes()
        self.assertEqual(raw, FIXTURE.read_bytes())
        self.assertEqual(summary["report_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertNotIn("raw-secret", result.output)
        self.assertNotIn("rules", result.metadata)
        if os.name != "nt":
            self.assertEqual(
                Path(summary["report_artifact"]).parent.stat().st_mode & 0o777, 0o700
            )

    def test_repeated_imports_never_overwrite_and_preserve_original_after_source_changes(
        self,
    ):
        first = asyncio.run(self._execute({"report": "evidence.xml"}))
        second = asyncio.run(self._execute({"report": "evidence.xml"}))
        self.assertNotEqual(
            first.metadata["report_artifact"], second.metadata["report_artifact"]
        )
        (self.workspace / "evidence.xml").write_text("changed")
        self.assertEqual(
            Path(first.metadata["report_artifact"]).read_bytes(), FIXTURE.read_bytes()
        )

    def test_invalid_inputs_create_no_artifacts(self):
        for params in (
            {},
            {"report": " "},
            {"report": "bad\npath"},
            {"report": "../evidence.xml"},
            {"report": "evidence.xml", "max_controls": 201},
            {"report": "evidence.xml", "expected_profile": "wrong"},
            {"report": "evidence.xml", "result_id": "wrong"},
            {"report": "evidence.xml", "execute": True},
        ):
            with self.subTest(params=params):
                self.assertFalse(asyncio.run(self._execute(params)).success)
        self.assertFalse((self.workspace / "artifacts").exists())

    def test_relative_symlink_escape_rejected_but_absolute_import_is_supported(self):
        with tempfile.TemporaryDirectory() as external:
            source = Path(external) / "external.xml"
            source.write_bytes(FIXTURE.read_bytes())
            try:
                (self.workspace / "link.xml").symlink_to(source)
            except OSError:
                self.skipTest("symlinks are unavailable")
            self.assertFalse(asyncio.run(self._execute({"report": "link.xml"})).success)
            result = asyncio.run(self._execute({"report": str(source)}))
            self.assertTrue(result.success, result.error)
            self.assertTrue(
                Path(result.metadata["report_artifact"]).is_relative_to(self.workspace)
            )

    def test_artifact_directory_cannot_escape_through_a_symlink(self):
        with tempfile.TemporaryDirectory() as external:
            (self.workspace / "artifacts").mkdir()
            try:
                (self.workspace / "artifacts/compliance").symlink_to(
                    external, target_is_directory=True
                )
            except OSError:
                self.skipTest("symlinks are unavailable")
            self.assertFalse(
                asyncio.run(self._execute({"report": "evidence.xml"})).success
            )
            self.assertEqual(list(Path(external).iterdir()), [])

    def test_missing_workspace_fails_without_running_a_process(self):
        token = set_current_workspace(None)
        try:
            result = asyncio.run(self._execute({"report": str(FIXTURE)}))
        finally:
            reset_current_workspace(token)
        self.assertFalse(result.success)
        self.assertIn("workspace", result.error)

    def test_malformed_xml_does_not_echo_raw_report_content(self):
        (self.workspace / "broken.xml").write_text("<raw-secret-check-output")
        result = asyncio.run(self._execute({"report": "broken.xml"}))
        self.assertFalse(result.success)
        self.assertNotIn("raw-secret", result.error)

    def test_import_runs_off_the_shared_event_loop(self):
        with patch(
            "kerkov.tools.compliance_report_tool.asyncio.to_thread",
            side_effect=RuntimeError("worker"),
        ) as worker:
            result = asyncio.run(self._execute({"report": "evidence.xml"}))
        self.assertFalse(result.success)
        worker.assert_called_once()

    def test_approval_is_limited_to_the_selected_report_and_result(self):
        scope = self.tool.approval_scope({"report": "evidence.xml"})
        self.assertIsNone(scope.broad_params)
        self.assertEqual(scope.volatile_params, frozenset({"max_controls"}))
