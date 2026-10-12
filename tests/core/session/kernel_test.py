"""SessionKernel.build_loop_session assembles a turn from shared state + ports."""

import unittest
from typing import Callable, List, Optional

from kerkov.core.config_manager import ConfigManager
from kerkov.core.conversation_engine import ApprovalStrategy, OutputSink, QuietSink
from kerkov.core.credentials import get_credential_manager
from kerkov.core.session import SessionKernel, build_session_state
from kerkov.models.basic_models import LLMResponse, Message
from kerkov.providers.base_provider import BaseLLMProvider
from kerkov.tools.base_tool import BaseTool


class _FakeProvider(BaseLLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="test", model="fake-model")

    async def complete(
        self,
        messages: List[Message],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[List[BaseTool]] = None,
        on_delta: Optional[Callable[[str], None]] = None,
    ) -> LLMResponse:
        return LLMResponse(content="", model="fake-model")

    def validate_connection(self) -> bool:
        return True

    def list_models(self) -> List[str]:
        return [self.model]

    async def get_max_context_tokens(self) -> Optional[int]:
        return 100_000

    async def get_max_output_tokens(self) -> Optional[int]:
        return 2048


class _NullApproval(ApprovalStrategy):
    async def approve(self, tool, params):  # type: ignore[override]
        raise NotImplementedError


def _kernel() -> SessionKernel:
    config = ConfigManager(
        GOOGLE_API_KEY="test",
        AGENT_TEMPERATURE=0.3,
        TOOL_OUTPUT_MAX_CHARS=4321,
    )
    state = build_session_state(config, get_credential_manager())
    state.provider_manager.set_provider(_FakeProvider())
    return SessionKernel(state)


class BuildLoopSessionTest(unittest.TestCase):

    def test_fills_provider_config_and_token_tracker_from_state(self):
        kernel = _kernel()
        output: OutputSink = QuietSink()
        session = kernel.build_loop_session(
            history=[],
            tools=[],
            approval=_NullApproval(),
            output=output,
        )
        self.assertIs(session.provider, kernel.state.provider_manager.provider)
        self.assertIs(session.token_tracker, kernel.state.token_tracker)
        self.assertIs(session.engagement_store, kernel.state.engagement_store)
        self.assertEqual(session.temperature, 0.3)
        self.assertEqual(session.tool_output_max_chars, 4321)
        self.assertEqual(session.max_iterations, 100)

    def test_passes_ports_and_defaults_through(self):
        kernel = _kernel()
        approval = _NullApproval()
        output = QuietSink()
        history: list = []
        session = kernel.build_loop_session(
            history=history,
            tools=[],
            approval=approval,
            output=output,
            interrupt_ends_turn=True,
        )
        self.assertIs(session.history, history)
        self.assertIs(session.approval, approval)
        self.assertIs(session.output, output)
        self.assertTrue(session.interrupt_ends_turn)
        # Defaults preserve the previous per-call behavior.
        self.assertEqual(session.transient_prompts, [])
        self.assertFalse(session.compaction_warning_shown)

    def test_requires_an_initialized_provider(self):
        config = ConfigManager(GOOGLE_API_KEY="test")
        state = build_session_state(config, get_credential_manager())
        kernel = SessionKernel(state)
        with self.assertRaises(AssertionError):
            kernel.build_loop_session(
                history=[], tools=[], approval=_NullApproval(), output=QuietSink()
            )


if __name__ == "__main__":
    unittest.main()
