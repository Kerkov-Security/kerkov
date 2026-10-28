"""Ansible tool approval, execution, profile and real local multi-host coverage."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from kerkov.core.automation.ansible import (
    AnsibleExecutor,
    ansible_binary,
    callback_directory,
    command_environment,
)
from kerkov.core.knowledge_base.models import LedgerEntry, LedgerSection
from kerkov.core.process.command_runner import CommandResult
from kerkov.core.run_workspace import reset_current_workspace, set_current_workspace
from kerkov.tools.ansible_tool import AnsibleTool


def _recap(failures=0, unreachable=0):
    return {
        "ok": 1,
        "changed": 0,
        "failures": failures,
        "unreachable": unreachable,
        "skipped": 0,
        "rescued": 0,
        "ignored": 0,
    }


class _FakeExecutor:
    def __init__(self, result=None, summary=None):
        self.result = result or CommandResult(0)
        self.summary = (
            summary
            if summary is not None
            else {
                "complete": True,
                "hosts": {"first": _recap(), "second": _recap()},
                "failed_tasks": [],
            }
        )
        self.calls = []
        self.write_summary = True

    async def run(self, args, directory, timeout):
        self.calls.append((args, directory, timeout))
        (directory / "ansible.log").write_text("raw secret debug output")
        if self.write_summary:
            (directory / "summary.json").write_text(json.dumps(self.summary))
        return self.result


class AnsibleToolTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        token = set_current_workspace(self.workspace)
        self.addCleanup(reset_current_workspace, token)
        self.playbook = self.workspace / "playbook.yml"
        self.playbook.write_text("- hosts: all\n  tasks: []\n")
        self.inventory = self.workspace / "hosts.json"
        self.inventory.write_text('{"all": {"hosts": {"first": {}, "second": {}}}}')
        self.executor = _FakeExecutor()
        self.tool = AnsibleTool(
            execution_enabled=True,
            executor=self.executor,
            binary="/bin/ansible-playbook",
        )
        self.params = {"playbook": "playbook.yml", "inventory": "hosts.json"}

    def test_explicit_inventory_and_bounded_recap_keep_complete_totals(self):
        result = asyncio.run(
            self.tool.execute(
                {**self.params, "max_hosts": 1, "tags": ["facts"], "diff": True}
            )
        )
        self.assertTrue(result.success, result.error)
        payload = json.loads(result.output)
        self.assertEqual(payload["host_count"], 2)
        self.assertEqual(payload["totals"]["ok"], 2)
        self.assertEqual(len(payload["hosts"]), 1)
        self.assertTrue(payload["hosts_truncated"])
        self.assertNotIn("raw secret", result.output)
        args, directory, _ = self.executor.calls[0]
        self.assertIn("--check", args)
        self.assertIn("--diff", args)
        self.assertIn("--tags=facts", args)
        self.assertTrue(directory.is_relative_to(self.workspace))
        if os.name != "nt":
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)

    def test_failed_and_unreachable_hosts_remain_visible(self):
        self.executor.summary["hosts"] = {
            "first": _recap(),
            "second": _recap(unreachable=1),
        }
        self.executor.result = CommandResult(4)
        result = asyncio.run(self.tool.execute(self.params))
        self.assertFalse(result.success)
        payload = json.loads(result.output)
        self.assertEqual(payload["hosts"][0]["host"], "second")
        self.assertEqual(payload["totals"]["unreachable"], 1)
        self.assertEqual(payload["exit_code"], 4)

    def test_default_recap_fits_engine_output_limit(self):
        self.executor.summary["hosts"] = {
            f"host_{index:02}": _recap(failures=int(index < 5)) for index in range(30)
        }
        self.executor.summary["failed_tasks"] = [
            {"host": f"host_{index:02}", "task": "Check connectivity", "kind": "failed"}
            for index in range(5)
        ]
        result = asyncio.run(self.tool.execute(self.params))
        self.assertLess(len(result.output), 6000)
        self.assertEqual(len(json.loads(result.output)["hosts"]), 30)

    def test_failed_task_details_share_the_host_limit(self):
        self.executor.summary["failed_tasks"] = [
            {"host": "first", "task": f"Task {index}", "kind": "failed"}
            for index in range(5)
        ]
        result = asyncio.run(self.tool.execute({**self.params, "max_hosts": 2}))
        payload = json.loads(result.output)
        self.assertEqual(len(payload["failed_tasks"]), 2)
        self.assertTrue(payload["failed_tasks_truncated"])
        full = json.loads(Path(result.metadata["summary_artifact"]).read_text())
        self.assertEqual(len(full["failed_tasks"]), 5)

    def test_missing_profile_returns_creation_guidance(self):
        ledger = AsyncMock()
        ledger.list.return_value = []
        tool = AnsibleTool(
            execution_enabled=True,
            ledger_getter=lambda: ledger,
            executor=self.executor,
            binary="fake",
        )
        for params in (
            {"action": "profile", "profile": "missing"},
            {"profile": "missing", "playbook": "facts"},
        ):
            with self.subTest(params=params):
                result = asyncio.run(tool.execute(params))
                self.assertFalse(result.success)
                self.assertIn("action=snapshot", result.error)
        self.assertEqual(self.executor.calls, [])

    def test_packaged_callback_exists(self):
        self.assertTrue((callback_directory() / "kerkov_summary.py").is_file())

    def test_no_hosts_or_incomplete_recaps_fail_loudly(self):
        for summary in (
            {"hosts": {}},
            {"complete": True, "hosts": {}},
            {"complete": True, "hosts": {"a": {"ok": -1}}},
        ):
            self.executor.summary = summary
            with self.subTest(summary=summary):
                result = asyncio.run(self.tool.execute(self.params))
                self.assertFalse(result.success)

    def test_execution_gate_prevents_every_side_effect(self):
        tool = AnsibleTool(executor=self.executor, binary="fake")
        result = asyncio.run(tool.execute(self.params))
        self.assertFalse(result.success)
        self.assertEqual(self.executor.calls, [])
        self.assertFalse((self.workspace / "artifacts").exists())

    def test_invalid_requests_never_execute(self):
        for params in (
            {},
            {**self.params, "profile": "lab"},
            {**self.params, "shell": "bad"},
            {**self.params, "limit": "all\n--extra-vars=x"},
            {**self.params, "forks": 100},
            {**self.params, "tags": ["one,two"]},
            {**self.params, "playbook": "../escape.yml"},
        ):
            with self.subTest(params=params):
                self.assertFalse(asyncio.run(self.tool.execute(params)).success)
        self.assertEqual(self.executor.calls, [])

    def test_profile_snapshot_caps_concurrency_and_uses_ledger(self):
        ledger = AsyncMock()
        ledger.list.return_value = [
            LedgerEntry(
                id=1,
                engagement_id="root",
                section=LedgerSection.ASSETS,
                entry={"kind": "ip", "value": "192.0.2.1"},
            )
        ]
        tool = AnsibleTool(
            execution_enabled=True,
            ledger_getter=lambda: ledger,
            executor=self.executor,
            binary="fake",
        )
        template = self.workspace / "profile.json"
        template.write_text(
            json.dumps(
                {
                    "name": "lab",
                    "hosts": [{"resource_id": 1, "alias": "server"}],
                    "playbooks": {"facts": "playbook.yml"},
                    "max_forks": 2,
                }
            )
        )
        created = asyncio.run(
            tool.execute({"action": "snapshot", "template": "profile.json"})
        )
        self.assertTrue(created.success, created.error)
        self.assertEqual(created.metadata["action"], "snapshot")
        loaded = asyncio.run(tool.execute({"action": "profile", "profile": "lab"}))
        self.assertTrue(loaded.success)
        self.assertEqual(loaded.metadata["action"], "profile")
        result = asyncio.run(
            tool.execute({"profile": "lab", "playbook": "facts", "forks": 10})
        )
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.metadata["forks"], 2)
        args = self.executor.calls[-1][0]
        inventory = json.loads(Path(args[args.index("--inventory") + 1]).read_text())
        self.assertEqual(
            inventory["all"]["hosts"]["server"]["ansible_host"], "192.0.2.1"
        )

    def test_missing_ledger_or_runtime_is_actionable(self):
        result = asyncio.run(self.tool.execute({"profile": "lab", "playbook": "facts"}))
        self.assertFalse(result.success)
        self.assertIn("ledger", result.error)
        with patch("kerkov.tools.ansible_tool.ansible_binary", return_value=None):
            tool = AnsibleTool(execution_enabled=True)
            result = asyncio.run(tool.execute(self.params))
            self.assertIn("setup ansible", result.error)

    def test_invalid_profile_never_echoes_secret_values(self):
        ledger = AsyncMock()
        ledger.list.return_value = []
        (self.workspace / "bad.json").write_text(
            json.dumps(
                {
                    "name": "lab",
                    "hosts": [
                        {"resource_id": 1, "alias": "a", "password": "do-not-echo"}
                    ],
                    "playbooks": {"facts": "playbook.yml"},
                }
            )
        )
        tool = AnsibleTool(execution_enabled=True, ledger_getter=lambda: ledger)
        result = asyncio.run(
            tool.execute({"action": "snapshot", "template": "bad.json"})
        )
        self.assertFalse(result.success)
        self.assertNotIn("do-not-echo", result.error + result.output)

    def test_approval_never_saves_a_mutable_playbook_grant(self):
        scope = self.tool.approval_scope(self.params)
        self.assertFalse(scope.saveable)
        self.assertIsNone(scope.broad_params)
        self.assertIn("timeout_seconds", scope.volatile_params)

    def test_environment_does_not_restore_credentials_or_ansible_overrides(self):
        directory = self.workspace / "run"
        directory.mkdir()
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "secret-value",
                "ANSIBLE_HOST_KEY_CHECKING": "False",
                "ANSIBLE_STDOUT_CALLBACK": "evil",
            },
        ):
            environment = command_environment(directory)
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("ANSIBLE_HOST_KEY_CHECKING", environment)
        self.assertNotIn("ANSIBLE_STDOUT_CALLBACK", environment)
        self.assertIn("BatchMode=yes", environment["ANSIBLE_SSH_ARGS"])

    def test_actual_child_cannot_inherit_ansible_overrides_or_credentials(self):
        directory = self.workspace / "child"
        directory.mkdir()
        environment_file = directory / "environment.json"
        script = (
            "import json, os, pathlib; "
            "pathlib.Path('environment.json').write_text(json.dumps(dict(os.environ)))"
        )
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "secret-value",
                "ANSIBLE_HOST_KEY_CHECKING": "False",
                "ANSIBLE_CALLBACKS_ENABLED": "evil",
                "ANSIBLE_CALLBACK_PLUGINS": "/evil",
                "ANSIBLE_CONFIG": "/evil/config",
                "KERKOV_CHILD_TEST": "inherited",
            },
        ):
            result = asyncio.run(
                AnsibleExecutor().run([sys.executable, "-c", script], directory, 5)
            )
        self.assertTrue(result.success, result.error)
        environment = json.loads(environment_file.read_text())
        self.assertNotIn("OPENAI_API_KEY", environment)
        for name in ("HOST_KEY_CHECKING", "CALLBACKS_ENABLED", "CALLBACK_PLUGINS"):
            self.assertNotIn("ANSIBLE_" + name, environment)
        self.assertEqual(environment["ANSIBLE_CONFIG"], str(directory / "ansible.cfg"))
        self.assertEqual(environment["KERKOV_CHILD_TEST"], "inherited")

    def test_startup_and_timeout_errors_are_returned_with_metadata(self):
        self.executor.write_summary = False
        for command in (
            CommandResult(None, error="Failed to start: Permission denied"),
            CommandResult(
                -9, timed_out=True, error="Ansible timed out after 1 seconds"
            ),
        ):
            with self.subTest(command=command):
                self.executor.result = command
                result = asyncio.run(self.tool.execute(self.params))
                self.assertFalse(result.success)
                self.assertIn(command.error, result.error)
                self.assertEqual(result.metadata["command_error"], command.error)
                self.assertIn(result.metadata["log_artifact"], result.error)

    def test_absent_callback_recap_reports_exit_code_and_log(self):
        self.executor.write_summary = False
        for exit_code in (0, 1, 4):
            with self.subTest(exit_code=exit_code):
                self.executor.result = CommandResult(exit_code)
                result = asyncio.run(self.tool.execute(self.params))
                self.assertFalse(result.success)
                self.assertIn(f"no recap (exit {exit_code})", result.error)
                self.assertIn(result.metadata["log_artifact"], result.error)
                self.assertNotIn("No such file", result.error)

    def test_vars_are_file_references_and_never_inline_command_secrets(self):
        (self.workspace / "vars.json").write_text('{"password": "do-not-print"}')
        result = asyncio.run(
            self.tool.execute(
                {**self.params, "vars_files": ["vars.json"], "check": False}
            )
        )
        self.assertTrue(result.success)
        args = self.executor.calls[0][0]
        self.assertNotIn("do-not-print", str(args) + result.output)
        self.assertNotIn("--check", args)
        self.assertEqual(
            args[args.index("--extra-vars") + 1],
            "@" + str(self.workspace / "vars.json"),
        )

    def test_timeout_result_is_not_a_successful_run(self):
        self.executor.result = CommandResult(None, timed_out=True)
        result = asyncio.run(self.tool.execute(self.params))
        self.assertFalse(result.success)
        self.assertTrue(result.metadata["timed_out"])


class AnsibleBinarySmokeTest(unittest.TestCase):
    def test_real_playbook_returns_two_local_host_recaps_and_hides_no_log(self):
        binary = ansible_binary()
        if binary is None:
            self.skipTest("ansible-playbook is not installed")
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir).resolve()
            inventory = {
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
            (workspace / "hosts.json").write_text(json.dumps(inventory))
            (workspace / "tasks.yml").write_text("""- hosts: all
  gather_facts: false
  tasks:
    - name: Verify local execution
      ansible.builtin.ping:
    - name: sensitive task name
      no_log: true
      ansible.builtin.fail:
        msg: secret-value-must-not-escape
      when: inventory_hostname == 'second'
""")
            token = set_current_workspace(workspace)
            try:
                result = asyncio.run(
                    AnsibleTool(execution_enabled=True, binary=binary).execute(
                        {
                            "playbook": "tasks.yml",
                            "inventory": "hosts.json",
                            "timeout_seconds": 30,
                        }
                    )
                )
            finally:
                reset_current_workspace(token)
            self.assertFalse(result.success)
            payload = json.loads(result.output)
            self.assertEqual(payload["host_count"], 2, result.error)
            self.assertEqual(payload["totals"]["failures"], 1)
            self.assertEqual(payload["failed_tasks"][0]["task"], "[no_log]")
            self.assertNotIn("secret-value-must-not-escape", result.output)
