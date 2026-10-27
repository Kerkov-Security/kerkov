"""Local Cinc evidence imports preserve uncertainty and private source artifacts."""

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

FIXTURE = Path(__file__).parents[1] / "core/compliance/fixtures/cinc.json"


class ComplianceReportToolTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        token = set_current_workspace(self.workspace)
        self.addCleanup(reset_current_workspace, token)
        (self.workspace / "evidence.json").write_bytes(FIXTURE.read_bytes())
        self.tool = ComplianceReportTool()

    def test_control_failures_are_data_and_full_evidence_survives_summary_clipping(
        self,
    ):
        result = asyncio.run(
            self.tool.execute({"report": "evidence.json", "max_controls": 1})
        )
        self.assertTrue(result.success, result.error)
        summary = json.loads(result.output)
        self.assertEqual(summary["counts"]["failed"], 1)
        self.assertEqual(len(summary["controls"]), 1)
        self.assertEqual(summary["controls"][0]["status"], "error")
        self.assertTrue(summary["controls_truncated"])
        full = json.loads(Path(summary["normalized_artifact"]).read_text())
        self.assertEqual(len(full["controls"]), 6)
        self.assertEqual(
            Path(summary["report_artifact"]).read_bytes(), FIXTURE.read_bytes()
        )
        self.assertEqual(
            summary["report_sha256"], hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
        )
        self.assertNotIn("raw-secret", result.output)
        self.assertNotIn("controls", result.metadata)
        if os.name != "nt":
            self.assertEqual(
                Path(summary["report_artifact"]).parent.stat().st_mode & 0o777, 0o700
            )

    def test_repeated_imports_never_overwrite_or_depend_on_the_original_file(self):
        first = asyncio.run(self.tool.execute({"report": "evidence.json"}))
        second = asyncio.run(self.tool.execute({"report": "evidence.json"}))
        self.assertNotEqual(
            first.metadata["report_artifact"], second.metadata["report_artifact"]
        )
        (self.workspace / "evidence.json").write_text("changed")
        self.assertEqual(
            Path(first.metadata["report_artifact"]).read_bytes(), FIXTURE.read_bytes()
        )

    def test_invalid_inputs_create_no_artifacts(self):
        for params in (
            {},
            {"report": " "},
            {"report": "bad\npath"},
            {"report": "../evidence.json"},
            {"report": "evidence.json", "max_controls": 201},
            {"report": "evidence.json", "execute": True},
        ):
            with self.subTest(params=params):
                self.assertFalse(asyncio.run(self.tool.execute(params)).success)
        self.assertFalse((self.workspace / "artifacts").exists())

    def test_rejected_evidence_is_preserved_and_referenced_without_raw_values(self):
        for raw, expected in (
            (b'{"raw-secret":', None),
            (FIXTURE.read_bytes(), "wrong"),
        ):
            with self.subTest(expected=expected):
                (self.workspace / "rejected.json").write_bytes(raw)
                result = asyncio.run(
                    self.tool.execute(
                        {
                            "report": "rejected.json",
                            "expected_profile": expected,
                        }
                    )
                )
                self.assertFalse(result.success)
                self.assertNotIn("raw-secret", result.error + result.output)
                self.assertEqual(
                    Path(result.metadata["report_artifact"]).read_bytes(), raw
                )
                self.assertEqual(
                    result.metadata["report_sha256"], hashlib.sha256(raw).hexdigest()
                )
                self.assertNotIn("normalized_artifact", result.metadata)

    def test_relative_symlink_escape_is_rejected_and_absolute_import_is_supported(self):
        with tempfile.TemporaryDirectory() as external:
            source = Path(external) / "external.json"
            source.write_bytes(FIXTURE.read_bytes())
            try:
                (self.workspace / "link.json").symlink_to(source)
            except OSError:
                self.skipTest("symlinks are unavailable")
            self.assertFalse(
                asyncio.run(self.tool.execute({"report": "link.json"})).success
            )
            result = asyncio.run(self.tool.execute({"report": str(source)}))
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
                asyncio.run(self.tool.execute({"report": "evidence.json"})).success
            )
            self.assertEqual(list(Path(external).iterdir()), [])

    def test_missing_workspace_and_malformed_json_fail_without_echoing_input(self):
        token = set_current_workspace(None)
        try:
            result = asyncio.run(self.tool.execute({"report": str(FIXTURE)}))
        finally:
            reset_current_workspace(token)
        self.assertFalse(result.success)
        self.assertIn("workspace", result.error)
        (self.workspace / "broken.json").write_text('{"raw-secret":')
        result = asyncio.run(self.tool.execute({"report": "broken.json"}))
        self.assertFalse(result.success)
        self.assertNotIn("raw-secret", result.error)

    def test_import_runs_off_the_event_loop(self):
        with patch(
            "kerkov.tools.compliance_report_tool.asyncio.to_thread",
            side_effect=RuntimeError("worker"),
        ) as worker:
            self.assertFalse(
                asyncio.run(self.tool.execute({"report": "evidence.json"})).success
            )
        worker.assert_called_once()

    def test_approval_is_limited_to_the_selected_report(self):
        scope = self.tool.approval_scope({"report": "evidence.json"})
        self.assertIsNone(scope.broad_params)
        self.assertEqual(scope.volatile_params, frozenset({"max_controls"}))
