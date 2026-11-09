"""Profile acquisition remains execution-gated and cannot save executable grants."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from kerkov.core.config_manager import ConfigManager
from kerkov.core.run_workspace import reset_current_workspace, set_current_workspace
from kerkov.tools.compliance_profile_tool import ComplianceProfileTool


def _create_mock_projects():
    return SimpleNamespace(
        list=Mock(return_value=[]),
        remote=SimpleNamespace(validate=AsyncMock(return_value={"name": "upstream"})),
        prepare=AsyncMock(
            return_value={"name": "upstream", "profile": "/cache/project"}
        ),
    )


def test_url_errors_precede_network_and_preparation(tmp_path):
    projects = _create_mock_projects()
    tool = ComplianceProfileTool(
        config=ConfigManager(compliance_projects_dir=str(tmp_path)), projects=projects
    )
    result = asyncio.run(
        tool.execute({"action": "validate", "url": "https://127.0.0.1/private"})
    )
    assert not result.success
    projects.remote.validate.assert_not_called()
    projects.prepare.assert_not_called()
    assert not tool.approval_scope({"action": "prepare"}).saveable


def test_preparation_requires_execution_enabled_and_active_workspace(tmp_path):
    projects = _create_mock_projects()
    tool = ComplianceProfileTool(
        config=ConfigManager(
            execution_enabled=False, compliance_projects_dir=str(tmp_path)
        ),
        projects=projects,
    )
    result = asyncio.run(
        tool.execute({"action": "prepare", "url": "https://github.com/example/project"})
    )
    assert not result.success and "EXECUTION_ENABLED" in result.error
    projects.prepare.assert_not_called()


@pytest.mark.skipif(
    os.name == "nt", reason="Profile preparation controller requires POSIX"
)
def test_shared_tool_can_prepare_and_refresh_selected_project(tmp_path):
    projects = _create_mock_projects()
    tool = ComplianceProfileTool(
        config=ConfigManager(
            execution_enabled=True, compliance_projects_dir=str(tmp_path)
        ),
        projects=projects,
    )
    token = set_current_workspace(tmp_path)
    try:
        result = asyncio.run(
            tool.execute(
                {
                    "action": "prepare",
                    "url": "https://github.com/example/project",
                    "refresh": True,
                    "commit": "a" * 40,
                }
            )
        )
        assert result.success
        assert projects.prepare.call_args.kwargs["refresh"] is True
        assert projects.prepare.call_args.kwargs["commit"] == "a" * 40
        assert result.metadata["profile"] == "/cache/project"
    finally:
        reset_current_workspace(token)
