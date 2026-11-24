"""The web console's typed /workflow command and the CTF engagement objective."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import pytest

from kerkov.core.config_manager import ConfigManager
from kerkov.core.engagement.engagement_store import EngagementStore
from kerkov.core.session.modes import ModeType
from kerkov.core.workflows.registry import CTF_OVERLAY
from kerkov.models.basic_models import LLMResponse, Message
from kerkov.providers.base_provider import BaseLLMProvider
from kerkov.tools.base_tool import BaseTool
from kerkov.web.runtime.events import WebEventBus
from kerkov.web.runtime.session import WebKerkovSession, web_command


class _RecordingProvider(BaseLLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="test", model="fake-model")
        self.requests: list[list[Message]] = []

    async def complete(
        self,
        messages: list[Message],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[list[BaseTool]] = None,
        on_delta: Optional[Callable[[str], None]] = None,
    ) -> LLMResponse:
        self.requests.append(messages)
        return LLMResponse(content="ok", model="fake-model")

    def validate_connection(self) -> bool:
        return True

    def list_models(self) -> list[str]:
        return [self.model]

    async def get_max_context_tokens(self) -> Optional[int]:
        return 100_000

    async def get_max_output_tokens(self) -> Optional[int]:
        return 2048


def _session(
    tmp_path: Path, provider: Optional[BaseLLMProvider] = None
) -> WebKerkovSession:
    config = ConfigManager(
        GOOGLE_API_KEY="test",
        EXECUTION_ENABLED=False,
        KB_ENABLED=False,
        WORKSPACE_DIR=str(tmp_path / "workspaces"),
        USER_APPROVALS_PATH=str(tmp_path / "approvals.yaml"),
        HTTP_PROFILES_PATH=str(tmp_path / "missing-http-profiles.yaml"),
    )
    session = WebKerkovSession(WebEventBus(), config=config)
    if provider is not None:
        session.provider_manager.set_provider(provider)
    session.engagement_store = EngagementStore(store_dir=tmp_path / "engagements")
    return session


def _result(session: WebKerkovSession) -> dict:
    events = [e for e in session.bus.drain_nowait() if e["type"] == "workflow.result"]
    assert events, "no workflow.result event emitted"
    return events[-1]["payload"]


@pytest.mark.parametrize(
    "command", ["/workflow", "/workflow list", "/workflow ctf start", "/workflows"]
)
def test_workflow_commands_are_local_not_stoppable(command: str) -> None:
    assert web_command(command) == "workflow"


@pytest.mark.asyncio
async def test_ctf_objective_rides_every_turn_until_stopped(tmp_path: Path) -> None:
    provider = _RecordingProvider()
    session = _session(tmp_path, provider)
    session.mode_manager.switch_mode(ModeType.RED)
    try:
        await session.handle_user_message("/workflow ctf start")
        payload = _result(session)
        assert payload["title"] == "CTF objective active"
        assert payload["workflow"] == "ctf"
        assert "targets Red" not in payload["detail"]
        engagement_id = session.engagement_store.engagement_id
        assert engagement_id is not None

        await session.handle_user_message("solve the web challenge")
        assert provider.requests[-1][-1].content == CTF_OVERLAY
        # The objective is transient: it never enters the saved history.
        assert all(m.content != CTF_OVERLAY for m in session.conversation_history)
        reader = EngagementStore(store_dir=tmp_path / "engagements")
        assert reader.load_engagement(engagement_id).metadata.workflow == "ctf"

        await session.handle_user_message("/workflow ctf stop")
        assert _result(session)["workflow"] is None
        await session.handle_user_message("next step")
        assert all(m.content != CTF_OVERLAY for m in provider.requests[-1])
    finally:
        session.close()


@pytest.mark.asyncio
async def test_clear_ends_the_objective_with_its_engagement(tmp_path: Path) -> None:
    session = _session(tmp_path, _RecordingProvider())
    try:
        await session.handle_user_message("/workflow ctf start")
        payload = _result(session)
        assert "ctf targets Red; this engagement is in Purple" in payload["detail"]
        await session.handle_user_message("/clear")
        assert session.engagement_store.workflow is None
        await session.handle_user_message("/workflow ctf")
        assert _result(session)["title"] == "CTF objective not active"
    finally:
        session.close()


@pytest.mark.asyncio
async def test_ctf_start_needs_a_provider(tmp_path: Path) -> None:
    session = _session(tmp_path)
    try:
        await session.handle_user_message("/workflow ctf start")
        payload = _result(session)
        assert payload["failed"] is True
        assert payload["title"] == "CTF needs an engagement"
        assert session.engagement_store.engagement_id is None
    finally:
        session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command,title",
    [
        ("/workflow compliance assess", "Compliance runs from its panel"),
        ("/workflow recon", "Unknown workflow"),
        ("/workflow ctf go", "Invalid workflow command"),
    ],
)
async def test_other_workflow_commands_are_refused(
    tmp_path: Path, command: str, title: str
) -> None:
    session = _session(tmp_path, _RecordingProvider())
    try:
        await session.handle_user_message(command)
        payload = _result(session)
        assert payload["failed"] is True
        assert payload["title"] == title
        assert session.engagement_store.engagement_id is None
    finally:
        session.close()


@pytest.mark.asyncio
async def test_list_names_each_workflow_and_its_role(tmp_path: Path) -> None:
    session = _session(tmp_path)
    try:
        await session.handle_user_message("/workflow")
        detail = _result(session)["detail"]
        assert "compliance (Blue)" in detail and "ctf (Red)" in detail
    finally:
        session.close()
