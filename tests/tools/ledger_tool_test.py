"""Tests for LedgerTool — the write path over the findings ledger."""

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Optional

from kerkov.core.knowledge_base.engagement_kb import EngagementKnowledgeBase
from kerkov.core.knowledge_base.findings_ledger import FindingsLedger
from kerkov.core.knowledge_base.models import LedgerSection
from kerkov.tools.ledger_tool import PROFILE_PAGE_MAX_CHARS, LedgerTool


def _run(coro):
    return asyncio.run(coro)


def _create_mock_getter(ledger: Optional[FindingsLedger]):
    return lambda: ledger


class _LedgerToolTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.kb = EngagementKnowledgeBase("root-1", self.tmpdir)
        self.ledger = FindingsLedger(self.kb)
        self.tool = LedgerTool(
            ledger_getter=_create_mock_getter(self.ledger),
            engagement_id_getter=lambda: "agent-7",
        )

    def tearDown(self) -> None:
        self.kb.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _execute(self, **params):
        return _run(self.tool.execute(params))


class HappyPathTest(_LedgerToolTestCase):
    def test_add_then_list_then_remove(self):
        add = self._execute(
            op="add",
            section="credentials",
            entry={
                "username": "svc_sql",
                "secret": "S3cr3t!",
                "kind": "password",
                "host": "10.0.0.5",
            },
        )
        self.assertTrue(add.success)
        entry_id = add.metadata["entry_id"]

        listing = self._execute(op="list")
        self.assertTrue(listing.success)
        self.assertEqual(listing.metadata["count"], 1)
        self.assertIn("svc_sql", listing.output)

        removed = self._execute(op="remove", entry_id=entry_id)
        self.assertTrue(removed.success)
        self.assertEqual(self._execute(op="list").metadata["count"], 0)

    def test_add_records_under_engagement_id_getter(self):
        self._execute(op="add", section="hosts", entry={"ip": "10.0.0.5"})
        entries = _run(self.ledger.list(LedgerSection.HOSTS))
        self.assertEqual(entries[0].engagement_id, "agent-7")

    def test_list_empty_ledger(self):
        result = self._execute(op="list")
        self.assertTrue(result.success)
        self.assertEqual(result.metadata["count"], 0)


class ValidationTest(_LedgerToolTestCase):
    def test_add_without_section_errors(self):
        result = self._execute(op="add", entry={"ip": "10.0.0.5"})
        self.assertFalse(result.success)
        self.assertIn("section", result.error)

    def test_add_without_entry_errors(self):
        result = self._execute(op="add", section="hosts")
        self.assertFalse(result.success)
        self.assertIn("entry", result.error)

    def test_remove_without_entry_id_errors(self):
        result = self._execute(op="remove")
        self.assertFalse(result.success)
        self.assertIn("entry_id", result.error)

    def test_unknown_op_errors(self):
        result = self._execute(op="update", section="hosts", entry={"ip": "1.1.1.1"})
        self.assertFalse(result.success)

    def test_host_without_identity_errors(self):
        result = self._execute(op="add", section="hosts", entry={"os": "linux"})
        self.assertFalse(result.success)
        self.assertIn("hostname", result.error)

    def test_service_without_host_errors(self):
        result = self._execute(op="add", section="services", entry={"port": 22})
        self.assertFalse(result.success)
        self.assertIn("host", result.error)

    def test_service_without_port_errors(self):
        result = self._execute(op="add", section="services", entry={"host": "10.0.0.5"})
        self.assertFalse(result.success)
        self.assertIn("port", result.error)

    def test_credential_without_location_errors(self):
        result = self._execute(
            op="add", section="credentials", entry={"username": "root"}
        )
        self.assertFalse(result.success)
        self.assertIn("source", result.error)


class NoLedgerTest(unittest.TestCase):
    def test_missing_ledger_is_graceful_failure(self):
        tool = LedgerTool(
            ledger_getter=_create_mock_getter(None),
            engagement_id_getter=lambda: None,
        )
        result = _run(tool.execute({"op": "list"}))
        self.assertFalse(result.success)
        self.assertIn("no findings ledger", result.error.lower())


if __name__ == "__main__":
    unittest.main()


class ProfileTest(_LedgerToolTestCase):
    def test_profile_reads_existing_resource_and_rejects_non_resource(self):
        resource = self._execute(
            op="add", section="assets", entry={"kind": "domain", "value": "example.com"}
        )
        finding = self._execute(
            op="add",
            section="findings",
            entry={"title": "Weak TLS", "host": "example.com"},
        )
        profile = self._execute(op="profile", entry_id=resource.metadata["entry_id"])
        self.assertTrue(profile.success)
        self.assertEqual(
            json.loads(profile.output)["findings"][0]["entry"]["title"], "Weak TLS"
        )
        for entry_id in (finding.metadata["entry_id"], 999):
            invalid = self._execute(op="profile", entry_id=entry_id)
            self.assertFalse(invalid.success)
            self.assertIn(f"entry_id {entry_id}", invalid.error)
            self.assertIn("asset/target", invalid.error)
            self.assertIn("op=graph", invalid.error)
        self.assertFalse(self._execute(op="profile").success)

    def test_profile_pages_retain_every_finding_service_and_evidence_handle(self):
        resource = self._execute(
            op="add",
            section="assets",
            entry={
                "kind": "domain",
                "value": "example.com",
                "notes": "Operator context",
            },
        )
        entry_id = resource.metadata["entry_id"]
        for index in range(6):
            self._execute(
                op="add",
                section="services",
                entry={
                    "host": "example.com",
                    "port": 440 + index,
                    "service": f"service-{index}",
                },
            )
        for index in range(4):
            self._execute(
                op="add",
                section="findings",
                entry={
                    "host": "example.com",
                    "title": f"Finding {index}",
                    "description": f"Description {index}. " * 50,
                },
                artifact_ref=42 + index,
            )
        full = _run(self.ledger.profile(entry_id))
        collected = {
            key: []
            for key in (
                "records",
                "hosts",
                "services",
                "findings",
                "connections",
                "related_resources",
                "activity",
            )
        }
        offset = pages = 0
        while True:
            result = self._execute(op="profile", entry_id=entry_id, offset=offset)
            self.assertTrue(result.success, result.error)
            self.assertLessEqual(len(result.output), PROFILE_PAGE_MAX_CHARS)
            page = json.loads(result.output)
            self.assertEqual(page["counts"], full["counts"])
            self.assertNotIn("records", page["resource"])
            if pages == 0:
                self.assertTrue(page["findings"])
            pages += 1
            for section in collected:
                collected[section].extend(page[section])
            if page["next_offset"] is None:
                break
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
        self.assertGreater(pages, 1)
        for section in collected:
            self.assertEqual(collected[section], full[section])
        self.assertEqual(_run(self.ledger.profile(entry_id)), full)
        invalid = self._execute(op="profile", entry_id=entry_id, offset=999)
        self.assertFalse(invalid.success)
        self.assertIn("offset=0", invalid.error)

    def test_oversized_profile_fields_do_not_block_pagination_or_alter_identities(self):
        value = "https://example.com/" + "a" * 6000
        resource = self._execute(
            op="add",
            section="assets",
            entry={"kind": "url", "value": value, "notes": "n" * 4000},
        )
        entry_id = resource.metadata["entry_id"]
        self._execute(
            op="add",
            section="findings",
            entry={
                "host": value,
                "title": "Large finding",
                "description": "界" * 8000,
                "remediation": "r" * 8000,
            },
            artifact_ref=42,
        )
        offset = 0
        findings = []
        while True:
            result = self._execute(op="profile", entry_id=entry_id, offset=offset)
            self.assertTrue(result.success, result.error)
            self.assertLessEqual(len(result.output), PROFILE_PAGE_MAX_CHARS)
            page = json.loads(result.output)
            self.assertNotIn("value", page["resource"])
            self.assertEqual(page["resource"]["omitted_fields"], ["value"])
            findings.extend(page["findings"])
            if page["next_offset"] is None:
                break
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
        self.assertEqual(findings[0]["entry"]["title"], "Large finding")
        self.assertEqual(findings[0]["artifact_ref"], 42)
        self.assertIn("entry.description", findings[0]["omitted_fields"])
        self.assertIn("entry.host", findings[0]["omitted_fields"])
        self.assertEqual(
            _run(self.ledger.profile(entry_id))["resource"]["value"], value
        )
