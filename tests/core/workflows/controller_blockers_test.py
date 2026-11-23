"""Controller conditions shared by run preparation and the web console."""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kerkov.core.workflows.compliance import ComplianceRun, controller_blockers


def _create_mock_run(execution_enabled: bool) -> SimpleNamespace:
    config = SimpleNamespace(execution_enabled=execution_enabled)
    return SimpleNamespace(context=SimpleNamespace(config=config))


@pytest.mark.parametrize(
    "platform,execution_enabled,expected",
    [
        ("linux", True, []),
        ("linux", False, ["EXECUTION_ENABLED"]),
        ("win32", True, ["WSL"]),
        ("win32", False, ["EXECUTION_ENABLED", "WSL"]),
    ],
)
def test_blockers_name_disabled_execution_and_windows_controllers(
    platform, execution_enabled, expected
):
    config = SimpleNamespace(execution_enabled=execution_enabled)
    with patch("kerkov.core.workflows.compliance.sys.platform", platform):
        blockers = controller_blockers(config)
    assert len(blockers) == len(expected)
    for blocker, needle in zip(blockers, expected):
        assert needle in blocker


def test_prepare_raises_the_first_blocker_before_touching_the_engagement():
    run = _create_mock_run(execution_enabled=False)
    with patch("kerkov.core.workflows.compliance.sys.platform", "win32"):
        with pytest.raises(ValueError, match="EXECUTION_ENABLED"):
            asyncio.run(ComplianceRun.prepare(run))
