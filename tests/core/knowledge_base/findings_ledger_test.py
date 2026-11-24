"""Tests for FindingsLedger — dedup/merge, serialization, migration, tree sharing."""

import asyncio
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from kerkov.core.knowledge_base.engagement_kb import (
    SCHEMA_VERSION,
    EngagementKnowledgeBase,
    kb_path_for,
)
from kerkov.core.knowledge_base.findings_ledger import FindingsLedger
from kerkov.core.knowledge_base.models import LedgerSection
from kerkov.core.knowledge_base.ssh_access import project_ssh_access


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
    def test_list_selected_sections_excludes_unrelated_rows(self):
        asset = self._add(
            LedgerSection.ASSETS, {"kind": "domain", "value": "example.com"}
        )
        target = self._add(LedgerSection.TARGETS, {"kind": "ip", "value": "192.0.2.1"})
        self._add(LedgerSection.CREDENTIALS, {"username": "user", "secret": "private"})
        entries = _run(
            self.ledger.list(sections=(LedgerSection.ASSETS, LedgerSection.TARGETS))
        )
        self.assertEqual([row.id for row in entries], [asset, target])
        self.assertEqual(_run(self.ledger.list(sections=())), [])
        with self.assertRaises(ValueError):
            _run(
                self.ledger.list(
                    LedgerSection.ASSETS, sections=(LedgerSection.TARGETS,)
                )
            )

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
    def test_first_ssh_port_enriches_portless_credential_without_merging_other_ports(
        self,
    ):
        resource = self._add(LedgerSection.ASSETS, {"kind": "ip", "value": "10.0.0.5"})
        credential = {"username": "root", "host": "10.0.0.5", "kind": "ssh"}
        original = self._add(
            LedgerSection.CREDENTIALS,
            {**credential, "private_key_file": "/keys/root"},
            artifact_ref=42,
        )
        enriched = self._add(
            LedgerSection.CREDENTIALS,
            {**credential, "port": "22", "known_hosts_file": "/keys/trusted"},
            engagement_id="agent-1",
        )
        self.assertEqual(enriched, original)
        rows = _run(self.ledger.list())
        candidates = project_ssh_access(rows, resource)["candidates"]
        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0]["complete"])
        self.assertEqual(candidates[0]["fields"]["port"], 22)

        other = self._add(LedgerSection.CREDENTIALS, {**credential, "port": 2222})
        self.assertNotEqual(original, other)
        self.assertEqual(
            self._add(LedgerSection.CREDENTIALS, {**credential, "port": 22}), original
        )
        credentials = _run(self.ledger.list(LedgerSection.CREDENTIALS))
        self.assertEqual(len(credentials), 2)
        original_row = next(row for row in credentials if row.id == original)
        self.assertEqual(original_row.artifact_ref, 42)
        self.assertEqual(original_row.engagement_id, "root-1")

    def test_same_host_merges_and_enriches(self):
        self._add(LedgerSection.HOSTS, {"ip": "10.0.0.5", "hostname": "dc01"})
        self._add(LedgerSection.HOSTS, {"ip": "10.0.0.5", "os": "Windows"})

        entries = _run(self.ledger.list(LedgerSection.HOSTS))
        self.assertEqual(len(entries), 1)
        merged = entries[0].entry
        self.assertEqual(merged["hostname"], "dc01")
        self.assertEqual(merged["os"], "Windows")

    def test_ssh_port_does_not_absorb_an_untyped_credential_at_another_port(self):
        credential = {"username": "root", "host": "10.0.0.5"}
        original = self._add(
            LedgerSection.CREDENTIALS, {**credential, "port": 2222, "secret": "other"}
        )
        ssh = self._add(
            LedgerSection.CREDENTIALS, {**credential, "kind": "ssh", "port": 22}
        )
        self.assertNotEqual(original, ssh)
        rows = _run(self.ledger.list(LedgerSection.CREDENTIALS))
        self.assertEqual(len(rows), 2)
        self.assertNotIn("secret", next(row for row in rows if row.id == ssh).entry)

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


class HostIdentityTest(_LedgerTestCase):
    """Rows naming one machine by IP and by hostname are the same fact."""

    def _entries(self, section):
        return _run(self.ledger.list(section))

    def _link_dc01(self):
        return self._add(LedgerSection.HOSTS, {"ip": "10.0.0.5", "hostname": "dc01"})

    def test_hostname_only_write_enriches_the_host_its_ip_identifies(self):
        host_id = self._link_dc01()
        self._add(LedgerSection.HOSTS, {"hostname": "DC01", "os": "Windows"})
        self._add(LedgerSection.HOSTS, {"ip": "10.0.0.5", "note": "domain controller"})

        entries = self._entries(LedgerSection.HOSTS)
        self.assertEqual([e.id for e in entries], [host_id])
        self.assertEqual(entries[0].entry["os"], "Windows")
        self.assertEqual(entries[0].entry["note"], "domain controller")

    def test_service_resolves_through_a_known_host(self):
        self._link_dc01()
        service_id = self._add(
            LedgerSection.SERVICES, {"host": "dc01", "port": "445", "service": "smb"}
        )
        self._add(
            LedgerSection.SERVICES,
            {"host": "10.0.0.5", "port": "445", "version": "SMB 3.1.1"},
        )

        entries = self._entries(LedgerSection.SERVICES)
        self.assertEqual([e.id for e in entries], [service_id])
        self.assertEqual(entries[0].entry["service"], "smb")
        self.assertEqual(entries[0].entry["version"], "SMB 3.1.1")

    def test_finding_resolves_through_a_known_host(self):
        self._link_dc01()
        self._add(
            LedgerSection.FINDINGS,
            {"title": "SMB signing disabled", "host": "10.0.0.5"},
            artifact_ref=7,
        )
        self._add(
            LedgerSection.FINDINGS,
            {"title": "SMB signing disabled", "host": "dc01", "severity": "medium"},
        )

        entries = self._entries(LedgerSection.FINDINGS)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].entry["severity"], "medium")
        self.assertEqual(entries[0].artifact_ref, 7)

    def test_linking_a_host_folds_rows_already_split_across_its_names(self):
        by_name = self._add(
            LedgerSection.SERVICES,
            {"host": "dc01", "port": "445", "service": "smb"},
            artifact_ref=3,
        )
        self._add(LedgerSection.SERVICES, {"host": "10.0.0.5", "port": "445"})
        self._add(LedgerSection.SERVICES, {"host": "10.0.0.5", "port": "88"})
        finding = self._add(
            LedgerSection.FINDINGS, {"title": "SMB signing disabled", "host": "dc01"}
        )
        self._add(
            LedgerSection.FINDINGS,
            {"title": "smb signing disabled", "host": "10.0.0.5", "severity": "low"},
        )

        self._link_dc01()

        services = self._entries(LedgerSection.SERVICES)
        self.assertEqual(len(services), 2)
        smb = next(e for e in services if e.entry["port"] == "445")
        self.assertEqual(smb.id, by_name)
        self.assertEqual(smb.artifact_ref, 3)
        self.assertEqual(smb.entry["service"], "smb")
        findings = self._entries(LedgerSection.FINDINGS)
        self.assertEqual([e.id for e in findings], [finding])
        self.assertEqual(findings[0].entry["severity"], "low")

    def test_service_recording_both_names_absorbs_a_later_single_name_write(self):
        service_id = self._add(
            LedgerSection.SERVICES, {"host": "dc01", "ip": "10.0.0.5", "port": "22"}
        )
        self._add(LedgerSection.SERVICES, {"ip": "10.0.0.5", "port": "22", "v": "9"})

        entries = self._entries(LedgerSection.SERVICES)
        self.assertEqual([e.id for e in entries], [service_id])
        self.assertEqual(entries[0].entry["host"], "dc01")

    def test_hostname_on_two_addresses_joins_neither(self):
        self._add(LedgerSection.HOSTS, {"ip": "10.0.0.5", "hostname": "www"})
        self._add(LedgerSection.HOSTS, {"ip": "10.0.0.6", "hostname": "www"})
        self._add(LedgerSection.SERVICES, {"host": "10.0.0.5", "port": "443"})
        self._add(LedgerSection.SERVICES, {"host": "10.0.0.6", "port": "443"})
        self._add(LedgerSection.SERVICES, {"host": "www", "port": "443"})
        self._add(LedgerSection.HOSTS, {"hostname": "www", "note": "behind a LB"})

        self.assertEqual(len(self._entries(LedgerSection.HOSTS)), 3)
        self.assertEqual(len(self._entries(LedgerSection.SERVICES)), 3)

    def test_hostname_moving_to_a_new_address_does_not_join_the_two(self):
        self._link_dc01()
        self._add(LedgerSection.HOSTS, {"ip": "10.0.0.9", "hostname": "dc01"})

        hosts = self._entries(LedgerSection.HOSTS)
        self.assertEqual(sorted(e.entry["ip"] for e in hosts), ["10.0.0.5", "10.0.0.9"])

    def test_refold_leaves_rows_that_name_another_address(self):
        self._add(
            LedgerSection.SERVICES, {"host": "dc01", "ip": "10.0.0.7", "port": "445"}
        )
        self._add(LedgerSection.SERVICES, {"host": "10.0.0.5", "port": "445"})

        self._link_dc01()

        self.assertEqual(len(self._entries(LedgerSection.SERVICES)), 2)

    def test_reworded_findings_and_other_ports_stay_distinct(self):
        self._link_dc01()
        self._add(
            LedgerSection.FINDINGS, {"title": "SMB signing disabled", "host": "dc01"}
        )
        self._add(
            LedgerSection.FINDINGS,
            {"title": "SMB signing not required", "host": "10.0.0.5"},
        )
        self._add(LedgerSection.SERVICES, {"host": "dc01", "port": "445"})
        self._add(LedgerSection.SERVICES, {"host": "10.0.0.5", "port": "139"})

        self.assertEqual(len(self._entries(LedgerSection.FINDINGS)), 2)
        self.assertEqual(len(self._entries(LedgerSection.SERVICES)), 2)


class FindingCheckIdTest(_LedgerTestCase):
    """A finding's `check_id` identifies it ahead of its free-text title."""

    def _findings(self):
        return _run(self.ledger.list(LedgerSection.FINDINGS))

    def test_reworded_title_with_the_same_check_merges(self):
        first = self._add(
            LedgerSection.FINDINGS,
            {
                "title": "SMB signing disabled",
                "check_id": "smb-signing",
                "host": "10.0.0.5",
            },
        )
        self._add(
            LedgerSection.FINDINGS,
            {
                "title": "SMB signing not required",
                "check_id": "SMB-Signing",
                "host": "10.0.0.5",
                "severity": "medium",
            },
        )

        findings = self._findings()
        self.assertEqual([f.id for f in findings], [first])
        self.assertEqual(findings[0].entry["title"], "SMB signing not required")
        self.assertEqual(findings[0].entry["severity"], "medium")

    def test_check_learned_later_folds_into_the_titled_finding(self):
        first = self._add(
            LedgerSection.FINDINGS, {"title": "Log4Shell", "host": "app01"}
        )
        self._add(
            LedgerSection.FINDINGS,
            {"title": "Log4Shell", "check_id": "CVE-2021-44228", "host": "app01"},
        )
        self._add(
            LedgerSection.FINDINGS,
            {"title": "Log4j JNDI RCE", "check_id": "CVE-2021-44228", "host": "app01"},
        )
        self._add(
            LedgerSection.FINDINGS,
            {"title": "Log4j JNDI RCE", "host": "app01", "status": "exploited"},
        )

        findings = self._findings()
        self.assertEqual([f.id for f in findings], [first])
        self.assertEqual(findings[0].entry["check_id"], "CVE-2021-44228")
        self.assertEqual(findings[0].entry["status"], "exploited")

    def test_same_title_with_different_checks_stays_distinct(self):
        for check_id in ("CVE-2023-0286", "CVE-2022-3602"):
            self._add(
                LedgerSection.FINDINGS,
                {"title": "Outdated OpenSSL", "check_id": check_id, "host": "web"},
            )
        self._add(
            LedgerSection.FINDINGS,
            {"title": "Outdated OpenSSL", "host": "web", "note": "which one?"},
        )

        findings = self._findings()
        self.assertEqual(len(findings), 3)
        checked = [f for f in findings if f.entry.get("check_id")]
        self.assertTrue(all("note" not in f.entry for f in checked))

    def test_check_matches_across_a_linked_hosts_names(self):
        first = self._add(
            LedgerSection.FINDINGS,
            {"title": "Anonymous FTP", "check_id": "ftp-anon", "host": "files"},
        )
        self._add(
            LedgerSection.FINDINGS,
            {
                "title": "FTP allows anonymous login",
                "check_id": "ftp-anon",
                "host": "10.0.0.8",
            },
        )
        self.assertEqual(len(self._findings()), 2)

        self._add(LedgerSection.HOSTS, {"ip": "10.0.0.8", "hostname": "files"})
        self.assertEqual([f.id for f in self._findings()], [first])

        self._add(
            LedgerSection.FINDINGS,
            {
                "title": "FTP anon",
                "check_id": "ftp-anon",
                "host": "files",
                "severity": "high",
            },
        )
        findings = self._findings()
        self.assertEqual([f.id for f in findings], [first])
        self.assertEqual(findings[0].entry["severity"], "high")

    def test_linking_hosts_folds_a_title_only_finding_into_its_check(self):
        first = self._add(
            LedgerSection.FINDINGS, {"title": "Anonymous FTP", "host": "files"}
        )
        self._add(
            LedgerSection.FINDINGS,
            {"title": "Anonymous FTP", "check_id": "ftp-anon", "host": "10.0.0.8"},
        )
        self.assertEqual(len(self._findings()), 2)

        self._add(LedgerSection.HOSTS, {"ip": "10.0.0.8", "hostname": "files"})

        findings = self._findings()
        self.assertEqual([f.id for f in findings], [first])
        self.assertEqual(findings[0].entry["check_id"], "ftp-anon")

    def test_check_answers_to_a_stored_address(self):
        first = self._add(
            LedgerSection.FINDINGS,
            {
                "title": "Anonymous FTP",
                "check_id": "ftp-anon",
                "host": "files",
                "ip": "10.0.0.8",
            },
        )

        self._add(
            LedgerSection.FINDINGS,
            {
                "title": "FTP allows anonymous login",
                "check_id": "ftp-anon",
                "host": "10.0.0.8",
                "severity": "high",
            },
        )

        findings = self._findings()
        self.assertEqual([f.id for f in findings], [first])
        self.assertEqual(findings[0].entry["severity"], "high")

    def test_linking_hosts_leaves_a_title_naming_two_checks(self):
        self._add(
            LedgerSection.FINDINGS,
            {"title": "Outdated OpenSSL", "host": "web", "note": "which one?"},
        )
        for check_id in ("CVE-2023-0286", "CVE-2022-3602"):
            self._add(
                LedgerSection.FINDINGS,
                {
                    "title": "Outdated OpenSSL",
                    "check_id": check_id,
                    "host": "10.0.0.9",
                },
            )

        self._add(LedgerSection.HOSTS, {"ip": "10.0.0.9", "hostname": "web"})

        findings = self._findings()
        self.assertEqual(len(findings), 3)
        checked = [f for f in findings if f.entry.get("check_id")]
        self.assertTrue(all("note" not in f.entry for f in checked))

    def test_check_renders_in_the_injected_block(self):
        self._add(
            LedgerSection.FINDINGS,
            {
                "title": "Log4Shell",
                "check_id": "CVE-2021-44228",
                "host": "app01",
                "severity": "critical",
            },
        )

        block = _run(self.ledger.serialize())
        self.assertIn("[critical] Log4Shell (CVE-2021-44228) @app01", block)
        self.assertNotIn("check_id=", block)


class HostIdentityProfileTest(_LedgerTestCase):
    """A fold across a machine's names keeps every name its rows were selected by."""

    def _target(self, kind, value):
        return self._add(LedgerSection.TARGETS, {"kind": kind, "value": value})

    def _profile_counts(self, entry_id):
        profile = _run(self.ledger.profile(entry_id))
        return profile["counts"]["services"], profile["counts"]["findings"]

    def test_linking_a_host_keeps_both_names_in_profiles(self):
        ip_target = self._target("ip", "192.0.2.8")
        domain_target = self._target("domain", "files.example")
        for host in ("192.0.2.8", "files.example"):
            self._add(LedgerSection.SERVICES, {"host": host, "port": "21"})
            self._add(LedgerSection.FINDINGS, {"title": "Anonymous FTP", "host": host})

        self._add(LedgerSection.HOSTS, {"ip": "192.0.2.8", "hostname": "files.example"})

        self.assertEqual(len(_run(self.ledger.list(LedgerSection.SERVICES))), 1)
        self.assertEqual(len(_run(self.ledger.list(LedgerSection.FINDINGS))), 1)
        self.assertEqual(self._profile_counts(ip_target), (1, 1))
        self.assertEqual(self._profile_counts(domain_target), (1, 1))

    def test_finding_matched_through_a_known_host_keeps_both_names(self):
        ip_target = self._target("ip", "192.0.2.8")
        domain_target = self._target("domain", "files.example")
        self._add(LedgerSection.HOSTS, {"ip": "192.0.2.8", "hostname": "files.example"})
        self._add(
            LedgerSection.FINDINGS, {"title": "Anonymous FTP", "host": "192.0.2.8"}
        )
        self._add(
            LedgerSection.FINDINGS, {"title": "Anonymous FTP", "host": "files.example"}
        )
        self._add(
            LedgerSection.FINDINGS, {"title": "Anonymous FTP", "host": "192.0.2.8"}
        )

        self.assertEqual(len(_run(self.ledger.list(LedgerSection.FINDINGS))), 1)
        self.assertEqual(self._profile_counts(ip_target), (0, 1))
        self.assertEqual(self._profile_counts(domain_target), (0, 1))

    def test_single_name_write_keeps_the_hostname_a_service_recorded(self):
        ip_target = self._target("ip", "192.0.2.8")
        domain_target = self._target("domain", "files.example")
        self._add(
            LedgerSection.SERVICES,
            {"host": "files.example", "ip": "192.0.2.8", "port": "21"},
        )
        self._add(
            LedgerSection.SERVICES, {"host": "192.0.2.8", "port": "21", "v": "3.0"}
        )

        services = _run(self.ledger.list(LedgerSection.SERVICES))
        self.assertEqual(len(services), 1)
        self.assertEqual(services[0].entry["v"], "3.0")
        self.assertEqual(self._profile_counts(ip_target), (1, 0))
        self.assertEqual(self._profile_counts(domain_target), (1, 0))

    def test_two_hostnames_on_one_address_are_not_folded_into_one(self):
        first = self._target("domain", "files.example")
        second = self._target("domain", "ftp.example")
        self._add(
            LedgerSection.SERVICES,
            {"host": "files.example", "ip": "192.0.2.8", "port": "21"},
        )
        self._add(
            LedgerSection.SERVICES,
            {"host": "ftp.example", "ip": "192.0.2.8", "port": "21"},
        )

        self.assertEqual(self._profile_counts(first), (1, 0))
        self.assertEqual(self._profile_counts(second), (1, 0))


class SerializationTest(_LedgerTestCase):
    def test_profile_detail_fields_do_not_evict_host_and_service_context(self):
        for index in range(20):
            self._add(
                LedgerSection.FINDINGS,
                {
                    "title": f"Finding {index}",
                    "host": "example.com",
                    "severity": "high",
                    "status": "open",
                    "description": "Long description. " * 300,
                    "remediation": "Long remediation. " * 300,
                },
            )
        technologies = ["nginx", "OpenSSL"] + ["x" * 200] * 40
        self._add(
            LedgerSection.HOSTS,
            {"hostname": "example.com", "technologies": technologies},
        )
        self._add(
            LedgerSection.SERVICES,
            {"host": "example.com", "port": 443, "technologies": technologies},
        )
        block = _run(self.ledger.serialize())
        self.assertIn("HOSTS", block)
        self.assertIn("SERVICES", block)
        self.assertIn('status="open"', block)
        self.assertIn('technologies=["nginx", "OpenSSL"', block)
        self.assertIn("description=[read full text with ledger op=list]", block)
        self.assertIn("remediation=[read full text with ledger op=list]", block)
        self.assertNotIn("Long description", block)
        self.assertNotIn("entries omitted", block)
        self.assertLess(len(block.encode()), 8000)
        stored = _run(self.ledger.list(LedgerSection.FINDINGS))
        self.assertEqual(stored[0].entry["description"], "Long description. " * 300)
        self.assertEqual(
            _run(self.ledger.list(LedgerSection.HOSTS))[0].entry["technologies"],
            technologies,
        )

    def test_resource_context_is_compact_and_full_notes_stay_in_store(self):
        notes = "Operator context\nHOSTS\n  - fake host\n" + "x" * 4000
        tags = [str(i) + "t" * 60 for i in range(20)]
        for i in range(4):
            self._add(
                LedgerSection.TARGETS,
                {
                    "kind": "ip",
                    "value": f"192.0.2.{i + 1}",
                    "environment": "lab",
                    "tags": tags,
                    "notes": notes,
                },
            )
        self._add(LedgerSection.HOSTS, {"ip": "192.0.2.10"})
        self._add(LedgerSection.SERVICES, {"host": "192.0.2.10", "port": 8080})

        block = _run(self.ledger.serialize())
        self.assertLess(len(block.encode()), 2000)
        self.assertNotIn("fake host", block)
        self.assertEqual(block.splitlines().count("HOSTS"), 1)
        self.assertIn("SERVICES", block)
        self.assertIn('environment="lab"', block)
        self.assertIn("ledger op=list", block)
        self.assertNotIn("entries omitted", block)
        rows = _run(self.ledger.list(LedgerSection.TARGETS))
        self.assertTrue(all(row.entry["notes"] == notes for row in rows))
        self.assertTrue(all(row.entry["tags"] == tags for row in rows))

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
        self.assertNotIn("10.0.0.0 ", _run(ledger.serialize()))
        self.assertEqual(len(_run(ledger.list(LedgerSection.HOSTS))), 60)

    def test_trimming_keeps_the_most_recently_updated_entries(self):
        ledger = FindingsLedger(self.kb, max_injected_bytes=400)
        self._fill_hosts(60)

        block = _run(ledger.serialize())
        self.assertIn("10.0.0.59", block)
        self.assertNotIn("10.0.0.0 ", block)

        self._add(LedgerSection.HOSTS, {"ip": "10.0.0.0", "os": "windows"})
        self.assertIn("10.0.0.0 ", _run(ledger.serialize()))

    def test_small_ledger_is_not_truncated(self):
        ledger = FindingsLedger(self.kb, max_injected_bytes=400)
        self._fill_hosts(3)
        block = _run(ledger.serialize())
        self.assertNotIn("omitted", block)
        self.assertIn("10.0.0.2", block)

    def test_reaches_routes_trimmed_before_host_and_service_enumeration(self):
        ledger = FindingsLedger(self.kb, max_injected_bytes=600)
        src = self._add(
            LedgerSection.ASSETS, {"kind": "domain", "value": "runner.example"}
        )
        tgt = self._add(
            LedgerSection.TARGETS, {"kind": "domain", "value": "api.example"}
        )
        self._fill_hosts(4)
        for i in range(4):
            self._add(LedgerSection.SERVICES, {"host": f"10.0.0.{i}", "port": 1000 + i})
        for port in range(1, 25):
            _run(
                ledger.add_connection(
                    {
                        "source_id": src,
                        "target_id": tgt,
                        "relation": "reaches",
                        "provenance": "declared",
                        "protocol": "tcp",
                        "port": port,
                        "note": "Configured direct connection within the scope.",
                    }
                )
            )
        block = _run(ledger.serialize())
        self.assertLessEqual(len(block), 600)
        self.assertIn("omitted", block)
        # Every host and service survives; the far more numerous reaches rows
        # drop first. Without the demotion, CONNECTIONS would outrank HOSTS and
        # SERVICES and evict the enumeration these routes merely summarize.
        for i in range(4):
            self.assertIn(f"10.0.0.{i} os=linux", block)
            self.assertIn(f"10.0.0.{i}:{1000 + i}", block)
        self.assertLess(block.count("--reaches-->"), 24)


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
        self.assertEqual(version, SCHEMA_VERSION)

    def test_v2_url_rows_with_default_ports_fold_into_portless_identity(self):
        tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        kb = EngagementKnowledgeBase("root-1", tmpdir)
        ledger = FindingsLedger(kb)

        def stored(section, value, **extra):
            # Written as schema v2 did: identity keeps the explicit port.
            return _run(
                kb.add_ledger_entry(
                    engagement_id="root-1",
                    section=section,
                    entry={"kind": "url", "value": value, **extra},
                    dedup_key=json.dumps(["url", value]),
                )
            )

        repo = self._add_repository(ledger)
        evidence = _run(kb.add_artifact(engagement_id="root-1", body="banner"))
        legacy = stored(
            LedgerSection.TARGETS, "https://example.test:443/app", name="Legacy"
        )
        portless = stored(LedgerSection.TARGETS, "https://example.test/app")
        _run(
            kb.add_ledger_entry(
                engagement_id="agent-1",
                section=LedgerSection.TARGETS,
                entry={"kind": "url", "value": "https://example.test/app"},
                dedup_key=json.dumps(["url", "https://example.test/app"]),
                artifact_ref=evidence,
            )
        )
        lone = stored(LedgerSection.ASSETS, "http://example.test:80/")
        custom = stored(LedgerSection.TARGETS, "https://example.test:8443/app")
        twin = stored(LedgerSection.ASSETS, "https://example.test/app")
        for target in (portless, twin):
            _run(
                ledger.add_connection(
                    {
                        "source_id": repo,
                        "target_id": target,
                        "relation": "deploys_to",
                        "provenance": "declared",
                        "note": "pipeline",
                    }
                )
            )
        kb.close()
        path = kb_path_for(tmpdir, "root-1")
        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA user_version=2")
        conn.commit()
        conn.close()

        kb = EngagementKnowledgeBase("root-1", tmpdir)
        self.addCleanup(kb.close)
        ledger = FindingsLedger(kb)
        rows = {row.id: row for row in _run(ledger.list())}

        self.assertNotIn(portless, rows)
        self.assertEqual(
            rows[legacy].entry,
            {"kind": "url", "value": "https://example.test/app", "name": "Legacy"},
        )
        self.assertEqual(rows[legacy].engagement_id, "root-1")
        self.assertEqual(rows[legacy].artifact_ref, evidence)
        self.assertEqual(rows[lone].entry["value"], "http://example.test/")
        self.assertEqual(rows[custom].entry["value"], "https://example.test:8443/app")
        self.assertEqual(rows[twin].entry["value"], "https://example.test/app")
        connections = [
            row.entry
            for row in rows.values()
            if row.section is LedgerSection.CONNECTIONS
        ]
        # Both edges collapse onto the oldest row holding the identity.
        self.assertEqual(
            [(c["source_id"], c["target_id"]) for c in connections], [(repo, legacy)]
        )

        # New writes under either spelling now reach the migrated rows.
        self.assertEqual(
            self._add_url(ledger, LedgerSection.TARGETS, "https://example.test/app"),
            legacy,
        )
        self.assertEqual(
            self._add_url(ledger, LedgerSection.ASSETS, "http://example.test:80/"),
            lone,
        )
        self.assertEqual(
            _run(
                ledger.save_resource(
                    LedgerSection.TARGETS,
                    {"kind": "url", "value": "https://example.test:443/app"},
                    entry_id=legacy,
                )
            ),
            legacy,
        )
        self.assertEqual(len(_run(ledger.list(LedgerSection.CONNECTIONS))), 1)

    @staticmethod
    def _add_repository(ledger):
        return _run(
            ledger.add(
                LedgerSection.ASSETS,
                {"kind": "repository", "value": str(Path.home() / "app")},
                engagement_id="root-1",
            )
        )

    @staticmethod
    def _add_url(ledger, section, value):
        return _run(
            ledger.add(section, {"kind": "url", "value": value}, engagement_id="x")
        )


if __name__ == "__main__":
    unittest.main()
