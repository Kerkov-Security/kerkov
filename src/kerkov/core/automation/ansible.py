"""Controlled Ansible CLI invocation and per-host recap processing."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from kerkov.core.automation.profiles import confined_path
from kerkov.core.credentials import sanitized_subprocess_environment
from kerkov.core.process.command_runner import (
    CommandResult,
    CommandRunner,
    SubprocessCommandRunner,
)


def callback_directory() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "ansible" / "callbacks"


def ansible_binary() -> str | None:
    if sys.platform == "win32":
        return None
    return shutil.which("ansible-playbook")


def run_directory(workspace: Path) -> Path:
    root = confined_path(workspace, "artifacts/ansible")
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="run-", dir=root))


def command_environment(directory: Path) -> dict[str, str]:
    config = directory / "ansible.cfg"
    config.write_text(
        "[defaults]\n"
        "stdout_callback = default\n"
        "callbacks_enabled = kerkov_summary\n"
        f"callback_plugins = {callback_directory()}\n"
        "host_key_checking = True\n"
        "retry_files_enabled = False\n"
        "display_args_to_stdout = False\n"
        "[inventory]\n"
        "enable_plugins = yaml, ini\n"
    )
    environment = {
        key: value
        for key, value in sanitized_subprocess_environment().items()
        if not key.startswith("ANSIBLE_")
    }
    environment.update(
        {
            "ANSIBLE_CONFIG": str(config),
            "ANSIBLE_NOCOLOR": "1",
            "ANSIBLE_LOCAL_TEMP": str(directory / "tmp"),
            "ANSIBLE_SSH_ARGS": "-o BatchMode=yes -o ControlMaster=no -o ControlPersist=no",
            "KERKOV_ANSIBLE_SUMMARY_FILE": str(directory / "summary.json"),
        }
    )
    return environment


class AnsibleExecutor:
    """Reuse the shared command runner for bounded, cancellable playbook runs."""

    def __init__(self, runner: CommandRunner | None = None) -> None:
        self.runner = runner or SubprocessCommandRunner()

    async def run(
        self, args: list[str], directory: Path, timeout: float
    ) -> CommandResult:
        return await self.runner.run(
            args,
            cwd=directory,
            timeout_seconds=timeout,
            log_path=directory / "ansible.log",
            environment=await asyncio.to_thread(command_environment, directory),
            display_name="Ansible playbook",
            inherit_environment=False,
        )


def load_summary(directory: Path, max_hosts: int) -> dict[str, Any]:
    with (directory / "summary.json").open("rb") as handle:
        raw = handle.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("Ansible summary exceeds the 4 MiB processing limit")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("complete") is not True:
        raise ValueError("Ansible did not produce a complete recap")
    hosts = payload.get("hosts")
    if not isinstance(hosts, dict) or not hosts:
        raise ValueError("Ansible did not report any matched hosts")
    counters = (
        "ok",
        "changed",
        "failures",
        "unreachable",
        "skipped",
        "rescued",
        "ignored",
    )
    totals = dict.fromkeys(counters, 0)
    normalized: list[dict[str, Any]] = []
    for host, recap in hosts.items():
        if not isinstance(recap, dict) or not isinstance(host, str):
            raise ValueError("invalid Ansible host recap")
        counts = {}
        for counter in counters:
            value = recap.get(counter, 0)
            if type(value) is not int or value < 0:
                raise ValueError("invalid Ansible recap counters")
            counts[counter] = value
            totals[counter] += value
        normalized.append({"host": host[:256], **counts})
    normalized.sort(
        key=lambda item: (-item["unreachable"], -item["failures"], item["host"])
    )
    failures = payload.get("failed_tasks", [])
    if not isinstance(failures, list):
        raise ValueError("invalid failed task recap")
    failed_tasks = []
    for failure in failures[:max_hosts]:
        if not isinstance(failure, dict):
            raise ValueError("invalid failed task recap")
        failed_tasks.append(
            {key: str(failure.get(key, ""))[:300] for key in ("host", "task", "kind")}
        )
    return {
        "host_count": len(hosts),
        "hosts": normalized[:max_hosts],
        "hosts_truncated": len(hosts) > max_hosts,
        "totals": totals,
        "failed_tasks": failed_tasks,
        "failed_tasks_truncated": bool(payload.get("failed_tasks_truncated"))
        or len(failures) > max_hosts,
    }
