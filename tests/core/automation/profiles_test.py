"""Engagement snapshots reuse ledger identities and freeze intended configuration."""

import json
import tempfile
import unittest
from pathlib import Path

from kerkov.core.automation.profiles import ProfileStore
from kerkov.core.knowledge_base.models import LedgerEntry, LedgerSection


def _rows(address="192.0.2.10"):
    return [
        LedgerEntry(
            id=1,
            engagement_id="root",
            section=LedgerSection.ASSETS,
            entry={"kind": "ip", "value": address},
        )
    ]


class ProfileStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.playbook = self.workspace / "facts.yml"
        self.playbook.write_text("- hosts: all\n  tasks: []\n")
        self.template = self.workspace / "template.json"
        self.data = {
            "name": "lab",
            "hosts": [
                {
                    "resource_id": 1,
                    "alias": "server",
                    "groups": ["linux"],
                    "user": "ubuntu",
                }
            ],
            "playbooks": {"facts": "facts.yml"},
        }
        self._write()
        self.store = ProfileStore(self.workspace)

    def _write(self):
        self.template.write_text(json.dumps(self.data))

    def test_snapshot_survives_template_changes_and_new_store_instance(self):
        created = self.store.create(self.template, _rows())
        self.data["max_forks"] = 1
        self._write()
        inventory, playbook, forks = ProfileStore(self.workspace).resolve(
            "lab", "facts", _rows()
        )
        self.assertEqual(forks, 5)
        self.assertEqual(playbook, self.playbook)
        self.assertEqual(
            inventory["all"]["hosts"]["server"]["ansible_host"], "192.0.2.10"
        )
        self.assertEqual(created, self.store.load("lab"))

    def test_cannot_overwrite_profile_or_use_changed_playbook(self):
        self.store.create(self.template, _rows())
        with self.assertRaisesRegex(ValueError, "already exists.*choose a new name"):
            self.store.create(self.template, _rows())
        self.playbook.write_text(
            "- hosts: all\n  tasks: [{ansible.builtin.ping: {}}]\n"
        )
        with self.assertRaisesRegex(ValueError, "playbook changed"):
            self.store.resolve("lab", "facts", _rows())

    def test_removed_or_changed_resource_requires_a_new_snapshot(self):
        self.store.create(self.template, _rows())
        for rows in ([], _rows("192.0.2.11")):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.store.resolve("lab", "facts", rows)

    def test_no_inferred_hosts_or_controller_connections(self):
        for hosts in (
            [{"resource_id": 99, "alias": "missing"}],
            [{"resource_id": 1, "alias": "remote", "connection": "local"}],
            [
                {"resource_id": 1, "alias": "first"},
                {"resource_id": 1, "alias": "second"},
            ],
        ):
            self.data["hosts"] = hosts
            self._write()
            with self.subTest(hosts=hosts), self.assertRaises(ValueError):
                self.store.create(self.template, _rows())

    def test_separate_engagement_workspaces_do_not_share_profiles(self):
        self.store.create(self.template, _rows())
        another = self.workspace / "another"
        another.mkdir()
        with self.assertRaises(FileNotFoundError):
            ProfileStore(another).load("lab")

    def test_symlinked_storage_cannot_escape_engagement(self):
        with tempfile.TemporaryDirectory() as outside:
            (self.workspace / "automation").symlink_to(
                outside, target_is_directory=True
            )
            with self.assertRaises(ValueError):
                self.store.create(self.template, _rows())
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_one_address_can_bind_distinct_ssh_endpoints(self):
        self.data["hosts"] = [
            {"resource_id": 1, "alias": "first", "port": 2222},
            {"resource_id": 1, "alias": "second", "port": 2223},
        ]
        self._write()
        snapshot = self.store.create(self.template, _rows())
        self.assertEqual(len(snapshot["inventory"]["all"]["hosts"]), 2)

    def test_key_contents_and_unknown_fields_are_not_profile_input(self):
        self.data["hosts"][0]["password"] = "do-not-store"
        self._write()
        with self.assertRaises(ValueError):
            self.store.create(self.template, _rows())
