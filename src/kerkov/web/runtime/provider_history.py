"""Prepare conversation history for a smaller model without changing the session."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from kerkov.context.context_compactor import ContextCompactor
from kerkov.models.basic_models import Message

if TYPE_CHECKING:
    from kerkov.context.token_tracker import TokenTracker
    from kerkov.core.config_manager import ConfigManager
    from kerkov.core.knowledge_base.findings_ledger import FindingsLedger
    from kerkov.providers.base_provider import BaseLLMProvider
    from kerkov.tools.base_tool import BaseTool
    from kerkov.web.runtime.session import WebKerkovSession


@dataclass
class HistoryPreparationContext:
    """Detached state for checking saved history without changing a live session."""

    conversation_history: list[Message]
    tools: list[BaseTool]
    findings_ledger: FindingsLedger | None
    token_tracker: TokenTracker
    config: ConfigManager
    provider: BaseLLMProvider | None = None


async def prepare_history(
    session: WebKerkovSession | HistoryPreparationContext,
    max_context: int,
    max_output: int | None,
    *,
    use_compaction_threshold: bool = True,
) -> list[Message] | None:
    """Compact a detached history with the old provider before installing the new one.

    Providers expose no common tokenizer. Use a three-byte estimate including tool
    schemas, ledger, and replay blocks, together with the compaction safety margin.
    Leave room for the next request and reject switches that cannot reclaim enough.
    The authoritative token tracker is never replaced by this estimate.
    Resume can validate against the context window minus output reserve instead
    of the proactive compaction threshold.
    A detached context without a provider only validates fit; oversized history
    is rejected without attempting a summary through a potentially smaller model.
    """
    from kerkov.web.runtime.providers import ProviderSelectionError

    history = session.conversation_history
    if not history:
        return None
    overhead = sum(
        len(tool.get_generic_schema().model_dump_json().encode("utf-8"))
        for tool in session.tools
    )
    if session.findings_ledger is not None:
        overhead += len((await session.findings_ledger.serialize()).encode("utf-8"))

    def footprint(messages: list[Message]) -> int:
        return overhead + sum(
            ContextCompactor._message_size_bytes(message) + 32 for message in messages
        )

    # Once summaries exist, the tracker may still describe a pre-compaction
    # history. Only calibrate untouched history; cap at one token per byte.
    tokens_per_byte = 1 / 3
    if not any(message.summary_level is not None for message in history):
        tokens_per_byte = max(
            tokens_per_byte,
            min(1, session.token_tracker.total_tokens / max(footprint(history), 1)),
        )

    def estimate(messages: list[Message]) -> int:
        return math.ceil(footprint(messages) * tokens_per_byte)

    reserve = max_output or session.config.summary_reserve_tokens
    budget = max_context - reserve
    if use_compaction_threshold:
        budget = min(budget, int(max_context * session.config.compaction_threshold_pct))
    if estimate(history) <= budget:
        return None
    failure = (
        "This conversation is too large for that model. "
        "Choose a larger-context model or start a new conversation."
    )
    if session.provider is None or budget <= 0:
        raise ProviderSelectionError(failure)

    compactor = ContextCompactor(
        session.token_tracker,
        max_context,
        session.config.compaction_threshold_pct,
        min(session.config.summary_reserve_tokens, max(1, budget // 4)),
        ledger_active=session.findings_ledger is not None,
    )
    # A recent window suitable for the old model may alone exceed the new one.
    fixed_size = footprint(history[: compactor.first_compactable_index(history)])
    recent_budget = max(1, int((budget / tokens_per_byte - fixed_size) / 2))
    compactor.PRESERVE_RECENT_BYTES = min(
        compactor.PRESERVE_RECENT_BYTES, recent_budget
    )
    provider = session.provider

    async def summarize(prompt: str) -> str:
        try:
            response = await provider.complete(
                messages=[Message(role="user", content=prompt)],
                temperature=0.0,
                max_tokens=compactor.summary_reserve,
            )
        except Exception as exc:
            raise ProviderSelectionError(
                "The current provider could not summarize this conversation for "
                "the switch. Check its access, choose a larger-context model, "
                "or start a new conversation."
            ) from exc
        if response.usage:
            session.token_tracker.record_side_spend(
                response.usage.prompt_tokens, response.usage.completion_tokens
            )
        if not response.content.strip():
            raise ProviderSelectionError(
                "The current provider returned an empty history summary. Try again."
            )
        return response.content

    prepared = await compactor.compact(list(history), summarize)
    if prepared is None or estimate(prepared) > budget:
        raise ProviderSelectionError(failure)
    return prepared
