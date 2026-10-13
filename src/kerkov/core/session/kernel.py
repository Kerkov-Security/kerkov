"""The session kernel: business logic shared by every front end.

Right now it owns one thing — assembling a turn's `LoopSession` from the shared
`SessionState` plus the front end's ports (output, approval, interrupt) and
runtime state (history, tools, KB, ledger, compactor). Both the CLI and the web
console built this ~20-field object identically; the kernel builds it once.
Later stages add lifecycle intents (clear, switch_mode, restore, ...) here.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from kerkov.context.context_compactor import ContextCompactor
from kerkov.core.cancellable_task import InterruptHandle
from kerkov.core.conversation_engine import ApprovalStrategy, LoopSession, OutputSink
from kerkov.core.knowledge_base.engagement_kb import EngagementKnowledgeBase
from kerkov.core.knowledge_base.findings_ledger import FindingsLedger
from kerkov.core.session.state import SessionState
from kerkov.models.basic_models import Message
from kerkov.tools.base_tool import BaseTool

MAX_TURN_ITERATIONS = 100


class SessionKernel:
    """Turns front-end requests into engine work against a `SessionState`."""

    def __init__(self, state: SessionState) -> None:
        self.state = state

    def build_loop_session(
        self,
        *,
        history: List[Message],
        tools: List[BaseTool],
        approval: ApprovalStrategy,
        output: OutputSink,
        interrupt: Optional[InterruptHandle] = None,
        knowledge_base: Optional[EngagementKnowledgeBase] = None,
        ledger: Optional[FindingsLedger] = None,
        compactor: Optional[ContextCompactor] = None,
        interrupt_ends_turn: bool = False,
        transient_prompts: Sequence[str] = (),
        compaction_warning_shown: bool = False,
    ) -> LoopSession:
        """Assemble a turn's `LoopSession`, filling config/provider from state."""
        state = self.state
        config = state.config
        provider = state.provider_manager.provider
        # Callers guard that a provider is initialized before starting a turn.
        assert provider is not None, "no provider initialized for this turn"
        return LoopSession(
            provider=provider,
            history=history,
            tools=tools,
            approval=approval,
            output=output,
            max_iterations=MAX_TURN_ITERATIONS,
            temperature=config.agent_temperature,
            max_response_tokens=config.max_response_tokens,
            engagement_store=state.engagement_store,
            knowledge_base=knowledge_base,
            ledger=ledger,
            kb_clip_threshold_bytes=config.kb_clip_threshold_bytes,
            tool_output_max_chars=config.tool_output_max_chars,
            tool_output_head_chars=config.tool_output_head_chars,
            tool_output_tail_chars=config.tool_output_tail_chars,
            compactor=compactor,
            token_tracker=state.token_tracker,
            interrupt=interrupt,
            interrupt_ends_turn=interrupt_ends_turn,
            transient_prompts=list(transient_prompts),
            compaction_warning_shown=compaction_warning_shown,
        )
