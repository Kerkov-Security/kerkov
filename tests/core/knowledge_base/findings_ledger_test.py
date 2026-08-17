"""Tests for FindingsLedger — dedup/merge, serialization, migration, tree sharing."""

import asyncio
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from kerkov.core.knowledge_base.engagement_kb import (
    EngagementKnowledgeBase,
    kb_path_for,
)
from kerkov.core.knowledge_base.findings_ledger import FindingsLedger
from kerkov.core.knowledge_base.models import LedgerSection


def _run(coro):
    return asyncio.run(coro)


class _LedgerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.kb = EngagementKnowledgeBase("root-1", self.tmpdir)
        self.ledger = FindingsLedger(self.kb)

    def tearDown(self) -> None:
        self.kb.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _add(self, section, entry, *, engagement_id="root-1", artifact_ref=None):
        return _run(
            self.ledger.add(
                section, entry, engagement_id=engagement_id, artifact_ref=artifact_ref
            )
        )


class CrudTest(_LedgerTestCase):
    def test_add_list_remove_roundtrip(self):
        entry_id = self._add(
            LedgerSection.CREDENTIALS,
            {"username": "svc_sql", "secret": "S3cr3t!", "kind": "password"},
        )
        entries = _run(self.ledger.list(LedgerSection.CREDENTIALS))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].entry["username"], "svc_sql")

        self.assertTrue(_run(self.ledger.remove(entry_id)))
        self.assertEqual(_run(self.ledger.list(LedgerSection.CREDENTIALS)), [])

    def test_remove_missing_returns_false(self):
        self.assertFalse(_run(self.ledger.remove(999)))


class DedupMergeTest(_LedgerTestCase):
    def test_same_host_merges_and_enriches(self):
        self._add(LedgerSection.HOSTS, {"ip": "10.0.0.5", "hostname": "dc01"})
        self._add(LedgerSection.HOSTS, {"ip": "10.0.0.5", "os": "Windows"})

        entries = _run(self.ledger.list(LedgerSection.HOSTS))
        self.assertEqual(len(entries), 1)
        merged = entries[0].entry
        self.assertEqual(merged["hostname"], "dc01")
        self.assertEqual(merged["os"], "Windows")

    def test_services_identity_is_host_and_port(self):
        self._add(LedgerSection.SERVICES, {"host": "10.0.0.5", "port": "445"})
        self._add(LedgerSection.SERVICES, {"host": "10.0.0.5", "port": "80"})
        self._add(
            LedgerSection.SERVICES,
            {"host": "10.0.0.5", "port": "445", "service": "smb"},
        )

        entries = _run(self.ledger.list(LedgerSection.SERVICES))
        self.assertEqual(len(entries), 2)
        smb = next(e for e in entries if e.entry.get("port") == "445")
        self.assertEqual(smb.entry["service"], "smb")

    def test_hostname_then_ip_folds_into_one_host(self):
        self._add(LedgerSection.HOSTS, {"hostname": "dc01", "os": "Windows"})
        self._add(LedgerSection.HOSTS, {"ip": "10.0.0.5", "hostname": "dc01"})

        entries = _run(self.ledger.list(LedgerSection.HOSTS))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].entry["ip"], "10.0.0.5")
        self.assertEqual(entries[0].entry["os"], "Windows")

    def test_credential_kind_learned_later_folds_into_one(self):
        self._add(LedgerSection.CREDENTIALS, {"username": "root", "host": "10.0.0.5"})
        self._add(
            LedgerSection.CREDENTIALS,
            {"username": "root", "host": "10.0.0.5", "kind": "password", "secret": "x"},
        )

        entries = _run(self.ledger.list(LedgerSection.CREDENTIALS))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].entry["secret"], "x")

    def test_merge_keeps_original_owner(self):
        self._add(LedgerSection.HOSTS, {"ip": "10.0.0.5"}, engagement_id="root-1")
        self._add(
            LedgerSection.HOSTS,
            {"ip": "10.0.0.5", "os": "Windows"},
            engagement_id="agent-9",
        )

        entries = _run(self.ledger.list(LedgerSection.HOSTS))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].engagement_id, "root-1")
        self.assertEqual(
            self.kb.delete_engagement_ledger_rows("agent-9"),
            0,
            "an enriching agent must not own — and take with it — the root's fact",
        )
        self.assertEqual(len(_run(self.ledger.list(LedgerSection.HOSTS))), 1)

    def test_merge_keeps_existing_artifact_ref(self):
        self._add(
            LedgerSection.FINDINGS,
            {"title": "SMB signing disabled", "host": "10.0.0.5"},
            artifact_ref=42,
        )
        self._add(
            LedgerSection.FINDINGS,
            {"title": "SMB signing disabled", "host": "10.0.0.5", "severity": "medium"},
        )

        entries = _run(self.ledger.list(LedgerSection.FINDINGS))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].artifact_ref, 42)
        self.assertEqual(entries[0].entry["severity"], "medium")

    def test_identity_missing_falls_back_to_content_hash(self):
        self._add(LedgerSection.NEXT_STEPS, {"step": "kerberoast svc_sql"})
        self._add(LedgerSection.NEXT_STEPS, {"step": "kerberoast svc_sql"})
        self._add(LedgerSection.NEXT_STEPS, {"step": "pivot to 10.0.0.6"})

        entries = _run(self.ledger.list(LedgerSection.NEXT_STEPS))
        self.assertEqual(len(entries), 2)


class SerializationTest(_LedgerTestCase):
    def test_empty_ledger_serializes_to_empty_string(self):
        self.assertEqual(_run(self.ledger.serialize()), "")

    def test_empty_sections_omitted(self):
        self._add(LedgerSection.FLAGS, {"value": "HTB{x}"})
        block = _run(self.ledger.serialize())
        self.assertIn("FLAGS", block)
        self.assertNotIn("HOSTS", block)
        self.assertNotIn("CREDENTIALS", block)

    def test_byte_stable_across_calls_without_writes(self):
        self._add(LedgerSection.HOSTS, {"ip": "10.0.0.5", "os": "Windows"})
        first = _run(self.ledger.serialize())
        second = _run(self.ledger.serialize())
        self.assertEqual(first, second)

    def test_changes_after_an_add(self):
        self._add(LedgerSection.HOSTS, {"ip": "10.0.0.5"})
        before = _run(self.ledger.serialize())
        self._add(LedgerSection.FLAGS, {"value": "HTB{x}"})
        after = _run(self.ledger.serialize())
        self.assertNotEqual(before, after)

    def test_artifact_ref_renders_as_artifact_reference(self):
        self._add(
            LedgerSection.FINDINGS,
            {"severity": "medium", "title": "SMB signing disabled", "host": "10.0.0.5"},
            artifact_ref=42,
        )
        block = _run(self.ledger.serialize())
        self.assertIn("[medium] SMB signing disabled @10.0.0.5", block)
        self.assertIn("artifact#42", block)


class InjectionBudgetTest(_LedgerTestCase):
    def _fill_hosts(self, count):
        for i in range(count):
            self._add(LedgerSection.HOSTS, {"ip": f"10.0.0.{i}", "os": "linux"})

    def test_block_stays_within_budget(self):
        ledger = FindingsLedger(self.kb, max_injected_bytes=400)
        self._fill_hosts(60)
        block = _run(ledger.serialize())
        self.assertLessEqual(len(block), 400)
        self.assertIn("omitted", block)

    def test_credentials_and_flags_outlive_host_enumeration(self):
        ledger = FindingsLedger(self.kb, max_injected_bytes=400)
        self._add(
            LedgerSection.CREDENTIALS,
            {"username": "svc_sql", "secret": "S3cr3t!", "host": "10.0.0.5"},
        )
        self._add(LedgerSection.FLAGS, {"value": "HTB{root}"})
        self._fill_hosts(60)

        block = _run(ledger.serialize())
        self.assertIn("S3cr3t!", block)
        self.assertIn("HTB{root}", block)

    def test_full_ledger_still_readable_through_list(self):
        ledger = FindingsLedger(self.kb, max_injected_bytes=400)
        self._fill_hosts(60)
        self.assertNotIn("10.0.0.59", _run(ledger.serialize()))
        self.assertEqual(len(_run(ledger.list(LedgerSection.HOSTS))), 60)

    def test_small_ledger_is_not_truncated(self):
        ledger = FindingsLedger(self.kb, max_injected_bytes=400)
        self._fill_hosts(3)
        block = _run(ledger.serialize())
        self.assertNotIn("omitted", block)
        self.assertIn("10.0.0.2", block)


class TreeSharingTest(_LedgerTestCase):
    def test_entries_from_different_engagements_all_listed(self):
        self._add(LedgerSection.HOSTS, {"ip": "10.0.0.5"}, engagement_id="root-1")
        self._add(LedgerSection.HOSTS, {"ip": "10.0.0.6"}, engagement_id="agent-9")

        entries = _run(self.ledger.list(LedgerSection.HOSTS))
        self.assertEqual(len(entries), 2)
        self.assertEqual({e.engagement_id for e in entries}, {"root-1", "agent-9"})


class MigrationTest(unittest.TestCase):
    def test_v1_db_migrates_and_keeps_artifacts(self):
        tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)

        # Build a schema-v1-shaped DB with an artifacts row and no ledger table.
        path = kb_path_for(tmpdir, "root-1")
        conn = sqlite3.connect(str(path))
        conn.executescript("""
            CREATE TABLE artifacts (
                id INTEGER PRIMARY KEY, engagement_id TEXT NOT NULL,
                kind TEXT NOT NULL, source TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
                metadata TEXT NOT NULL DEFAULT '{}', agent_type TEXT,
                size_bytes INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
            );
            INSERT INTO artifacts (engagement_id, kind, source, title, body,
                metadata, size_bytes, created_at)
            VALUES ('root-1', 'tool_output', 'exec', 'nmap', 'PORT 22 open',
                '{}', 12, '2026-01-01T00:00:00+00:00');
            """)
        conn.execute("PRAGMA user_version=1")
        conn.commit()
        conn.close()

        kb = EngagementKnowledgeBase("root-1", tmpdir)
        self.addCleanup(kb.close)

        # Connecting triggers _migrate; ledger table must appear.
        self.assertEqual(_run(kb.count()), 1)
        ledger = FindingsLedger(kb)
        _run(
            ledger.add(LedgerSection.HOSTS, {"ip": "10.0.0.5"}, engagement_id="root-1")
        )
        self.assertEqual(len(_run(ledger.list())), 1)

        check = sqlite3.connect(str(path))
        try:
            version = int(check.execute("PRAGMA user_version").fetchone()[0])
        finally:
            check.close()
        self.assertEqual(version, 2)


if __name__ == "__main__":
    unittest.main()
