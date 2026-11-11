"""Write a bounded recap without copying module results into model output."""

import json
import os
from pathlib import Path
from typing import Any

from ansible.plugins.callback import CallbackBase


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "aggregate"
    CALLBACK_NAME = "kerkov_summary"
    CALLBACK_NEEDS_ENABLED = True

    def __init__(self) -> None:
        super().__init__()
        self.failures = []
        self.failure_count = 0

    def _failure(self, result: Any, kind: str) -> None:
        self.failure_count += 1
        if len(self.failures) >= 200:
            return
        hidden = result._result.get("_ansible_no_log") or result._task.no_log
        self.failures.append(
            {
                "host": result._host.get_name()[:256],
                "task": "[no_log]" if hidden else result._task.get_name()[:300],
                "kind": kind,
            }
        )

    def v2_runner_on_failed(self, result: Any, ignore_errors: bool = False) -> None:
        self._failure(result, "ignored" if ignore_errors else "failed")

    def v2_runner_on_unreachable(self, result: Any) -> None:
        self._failure(result, "unreachable")

    def v2_playbook_on_stats(self, stats: Any) -> None:
        path = Path(os.environ["KERKOV_ANSIBLE_SUMMARY_FILE"])
        payload = {
            "complete": True,
            "hosts": {host: stats.summarize(host) for host in sorted(stats.processed)},
            "failed_tasks": self.failures,
            "failed_tasks_truncated": self.failure_count > len(self.failures),
        }
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
