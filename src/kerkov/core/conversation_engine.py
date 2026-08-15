"""
Provider-agnostic conversation engine.

`ConversationEngine.run_loop` is the tool-execution loop that was previously
inlined in `KerkovCLI.get_response`. It reads everything it needs from a
`LoopSession`, so the same loop can drive the interactive CLI master and an
isolated agent run with no divergence.

Two abstractions decouple the loop from the CLI:
- `OutputSink` — where narration goes (`ConsoleSink` for the master, `QuietSink`
  for agents).
- `ApprovalStrategy` — how a tool call is approved (`InteractiveApproval` for the
  master, `WhitelistAutoApproval` for agents).
"""

import asyncio
import contextlib
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, ContextManager, Coroutine, Iterator, List, Optional

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape

from kerkov.cli.approval_mode_manager import ApprovalModeManager
from kerkov.cli.cancellable_task import (
    InterruptedByUser,
    InterruptHandle,
    run_cancellable,
)
from kerkov.cli.engagement_store import EngagementStore
from kerkov.cli.tool_approval_helper import ToolApprovalCliHelper, ToolApprovalResult
from kerkov.context.context_compactor import ContextCompactor
from kerkov.context.token_tracker import TokenTracker
from kerkov.core.knowledge_base.engagement_kb import EngagementKnowledgeBase
from kerkov.core.knowledge_base.models import ArtifactKind
from kerkov.core.logging.logger_factory import get_logger
from kerkov.models.basic_models import LLMResponse, Message, TokenUsage, ToolCall
from kerkov.providers.base_provider import BaseLLMProvider
from kerkov.tools.base_tool import BaseTool, ToolResult

TerminateAfterToolFn = Callable[[ToolCall, ToolResult], bool]

logger = get_logger("ConversationEngine")

PREVIEW_HEAD_CHARS = 1500
PREVIEW_TAIL_CHARS = 500
KB_INGEST_SKIP_TOOLS = frozenset({"recall"})
_TITLE_PARAM_KEYS = ("command", "url", "file_path", "path", "query", "action")
_TOOL_ARTIFACT_KINDS = {
    "read": ArtifactKind.FILE,
    "http": ArtifactKind.HTTP_RESPONSE,
}


def _format_seconds(s: float) -> str:
    total = int(s)
    if total < 60:
        return f"{total}s"
    return f"{total // 60}m{total % 60}s"


class TurnTimer:
    """Tracks elapsed time for a single assistant turn."""

    def __init__(self) -> None:
        self._start: Optional[float] = None

    def start(self) -> None:
        self._start = time.monotonic()

    def elapsed(self) -> float:
        if self._start is None:
            return 0.0
        return time.monotonic() - self._start

    def formatted(self) -> str:
        return _format_seconds(self.elapsed())


NON_INTERACTIVE_RETRY_PROMPT = (
    "Execute the necessary tool calls now. "
    "Do not describe a plan, do not defer to a later turn, and do not ask for confirmation again "
    "unless the task is unsafe."
)


def extract_raw_blocks(raw_response: Any) -> Optional[List[Any]]:
    """Normalize provider-specific raw assistant blocks for conversation replay."""
    if raw_response is None:
        return None
    if isinstance(raw_response, dict):
        output = raw_response.get("output")
        return output if isinstance(output, list) else None
    return getattr(raw_response, "content", None)


def format_tool_result(tool_name: str, tool_result: ToolResult) -> str:
    """Format a tool result for adding to conversation history."""
    status = "Success" if tool_result.success else "Failed"
    result_parts = [
        f"Tool '{tool_name}' execution result:",
        f"Status: {status}",
        f"Output:\n{tool_result.output}",
    ]

    if tool_result.error:
        result_parts.append(f"Error: {tool_result.error}")

    if tool_result.metadata:
        result_parts.append(f"Metadata: {tool_result.metadata}")

    return "\n".join(result_parts)


def _summarize_parameters(params: Optional[dict]) -> dict:
    """Truncate tool parameters for artifact metadata.

    A parameter can itself be a large payload (an HTTP body, a file write), and
    the metadata column is not the place for a second copy of it.
    """
    summary: dict = {}
    for key, value in (params or {}).items():
        if isinstance(value, str) and len(value) > 200:
            summary[key] = value[:200] + "..."
        else:
            summary[key] = value
    return summary


def artifact_title(tool_call: ToolCall) -> str:
    """Short human label for a stored artifact, e.g. "exec: nmap -sV 10.0.0.5"."""
    params = tool_call.parameters or {}
    for key in _TITLE_PARAM_KEYS:
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            hint = " ".join(value.split())
            if len(hint) > 80:
                hint = hint[:77] + "..."
            return f"{tool_call.tool_name}: {hint}"
    return tool_call.tool_name


def artifact_kind_for_tool(tool_name: str) -> ArtifactKind:
    """Classify a tool's output so `recall(kind=...)` can narrow to it."""
    return _TOOL_ARTIFACT_KINDS.get(tool_name, ArtifactKind.TOOL_OUTPUT)


def format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def build_clipped_output(output: str, artifact_id: int) -> str:
    """Replace a large tool output with a reference plus a head/tail preview.

    The full text is already in the knowledge base by this point; what goes
    into history is the handle for getting it back plus enough of the content
    to judge whether that is worth doing.
    """
    size = len(output.encode("utf-8"))
    head = output[:PREVIEW_HEAD_CHARS]
    tail = output[-PREVIEW_TAIL_CHARS:] if len(output) > PREVIEW_HEAD_CHARS else ""
    elided = len(output) - len(head) - len(tail)

    parts = [
        f"[{format_bytes(size)} of output stored as artifact#{artifact_id} and "
        f"kept out of this conversation. Retrieve the full text with "
        f"recall(query='artifact#{artifact_id}'), or search it with "
        f"recall(query='<keywords>'). Preview follows.]",
        "",
        head,
    ]
    if tail:
        parts.extend(
            [
                f"\n[... {elided} characters elided — in artifact#{artifact_id} ...]\n",
                tail,
            ]
        )
    return "\n".join(parts)


def looks_like_deferred_execution(content: str) -> bool:
    """Heuristic for plan-only responses in non-interactive mode."""
    lowered = content.lower()
    markers = (
        "i'll ",
        "i will ",
        "let me ",
        "going to ",
        "about to ",
        "then ",
        "once it completes",
        "afterward",
        "after that",
        "i can ",
    )
    return any(marker in lowered for marker in markers)


class OutputSink(ABC):
    """Where the loop's narration is delivered."""

    @abstractmethod
    def status(self, label: str) -> ContextManager[Any]:
        """Context manager for a long-running operation (spinner / no-op)."""
        pass

    @abstractmethod
    def info(self, msg: str) -> None:
        """Low-emphasis progress line."""
        pass

    @abstractmethod
    def warning(self, msg: str) -> None:
        """Low-emphasis warning line."""
        pass

    @abstractmethod
    def error(self, msg: str) -> None:
        """Error line."""
        pass

    @abstractmethod
    def assistant_text(self, content: str) -> None:
        """Intermediate assistant text emitted alongside tool calls."""
        pass

    @abstractmethod
    def final(self, content: str) -> None:
        """The assistant's final response for the turn."""
        pass

    @abstractmethod
    def tool_start(self, tool_name: str, params: dict) -> None:
        """A tool call is about to be executed."""
        pass

    @abstractmethod
    def tool_succeeded(self, tool_name: str, result: ToolResult) -> None:
        """A tool call finished successfully."""
        pass

    @abstractmethod
    def tool_failed(self, tool_name: str, result: ToolResult) -> None:
        """A tool call finished with an error."""
        pass

    @abstractmethod
    def token_usage(self, usage: Optional[TokenUsage]) -> None:
        """Report per-turn token usage."""
        pass

    def start_turn(self) -> None:
        """Called at the beginning of each assistant turn."""

    def end_turn(self) -> None:
        """Called at the end of each assistant turn."""

    def tool_pending_approval(self, tool_name: str, params: dict) -> None:
        """Called just before a tool call is submitted for approval."""


@contextlib.contextmanager
def _make_timed_status(console: Console, label: str, timer: TurnTimer) -> Iterator[Any]:
    """Rich status with a live elapsed-time counter updated via asyncio callbacks.

    Falls back to a static label when no running event loop is detected.
    """
    phase_start = time.monotonic()
    handle: List[Any] = [None]

    with console.status("") as rich_status:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            rich_status.update(label)
            yield rich_status
            return

        def _refresh() -> None:
            phase_s = time.monotonic() - phase_start
            turn_s = timer.elapsed()
            rich_status.update(
                f"{label} [dim]({_format_seconds(phase_s)} · {_format_seconds(turn_s)} total)[/dim]"
            )
            handle[0] = loop.call_later(0.5, _refresh)

        handle[0] = loop.call_soon(_refresh)
        try:
            yield rich_status
        finally:
            if handle[0] is not None:
                handle[0].cancel()


class ConsoleSink(OutputSink):
    """`OutputSink` that renders to a Rich console — current master behavior."""

    def __init__(self, console: Console) -> None:
        self._console = console
        self._timer = TurnTimer()
        self._active_status: Optional[Any] = None

    def start_turn(self) -> None:
        self._timer.start()

    def end_turn(self) -> None:
        elapsed = self._timer.elapsed()
        if elapsed >= 1.0:
            self._console.print(f"[dim]↩ {_format_seconds(elapsed)}[/dim]")

    # tool_pending_approval intentionally prints nothing: the hook fires for
    # every tool call, including ones auto-approved by rule, and any notice
    # here would be stranded above the approval box that erases itself.

    def status(self, label: str) -> ContextManager[Any]:
        if not self._console.is_terminal:
            self._console.print(f"[dim]{label}[/dim]")
            return contextlib.nullcontext()
        return self._tracked_status(label)

    @contextlib.contextmanager
    def _tracked_status(self, label: str) -> Iterator[Any]:
        with _make_timed_status(self._console, label, self._timer) as status:
            self._active_status = status
            try:
                yield status
            finally:
                if self._active_status is status:
                    self._active_status = None

    @contextlib.contextmanager
    def suspend_status(self) -> Iterator[None]:
        """Temporarily give an interactive terminal prompt exclusive rendering."""

        status = self._active_status
        if status is None:
            yield
            return

        status.stop()
        try:
            yield
        finally:
            if self._active_status is status:
                status.start()

    def info(self, msg: str) -> None:
        self._console.print(f"[dim]{msg}[/dim]")

    def warning(self, msg: str) -> None:
        self._console.print(f"[dim yellow]{msg}[/dim yellow]")

    def error(self, msg: str) -> None:
        self._console.print(f"[red]{msg}[/red]")

    def assistant_text(self, content: str) -> None:
        self._console.print()
        self._console.print(Markdown(content))
        self._console.print()

    def final(self, content: str) -> None:
        self._console.print()
        self._console.print(Markdown(content))
        self._console.print()

    def tool_start(self, tool_name: str, params: dict) -> None:
        call = escape(f"{tool_name}({params})")
        self._console.print(f"[dim]→ Executing: {call}[/dim]")

    def tool_succeeded(self, tool_name: str, result: ToolResult) -> None:
        self._console.print("[dim green]✓ Command executed[/dim green]")
        if (
            tool_name == "persistent_shell_execute_cmd"
            and result.metadata
            and result.metadata.get("output_events_only")
        ):
            output_events_only = result.metadata["output_events_only"]
            trimmed = escape(output_events_only[:1000])
            suffix = "..." if len(output_events_only) > 1000 else ""
            self._console.print(f"[dim]{trimmed}{suffix}[/dim]")
        elif result.output:
            trimmed = escape(result.output[:1000])
            suffix = "..." if len(result.output) > 1000 else ""
            self._console.print(f"[dim]{trimmed}{suffix}[/dim]")

    def tool_failed(self, tool_name: str, result: ToolResult) -> None:
        self._console.print(f"[dim red]✗ Error: {escape(str(result.error))}[/dim red]")

    def token_usage(self, usage: Optional[TokenUsage]) -> None:
        if usage:
            self._console.print(
                f"[dim]Tokens: {usage.prompt_tokens} in, "
                f"{usage.completion_tokens} out, "
                f"{usage.total_tokens} total[/dim]"
            )


class QuietSink(OutputSink):
    """`OutputSink` for agent runs: logs + buffers, no console contention."""

    def __init__(self, agent_logger: Optional[Any] = None):
        self._logger = agent_logger or logger
        self.buffer: List[str] = []
        self._timer = TurnTimer()

    def start_turn(self) -> None:
        self._timer.start()

    def end_turn(self) -> None:
        self._record(f"turn elapsed: {self._timer.formatted()}")

    def _record(self, msg: str) -> None:
        self.buffer.append(msg)
        self._logger.info(msg)

    def status(self, label: str) -> ContextManager[Any]:
        return contextlib.nullcontext()

    def info(self, msg: str) -> None:
        self._record(msg)

    def warning(self, msg: str) -> None:
        self._record(f"WARNING: {msg}")

    def error(self, msg: str) -> None:
        self._record(f"ERROR: {msg}")

    def assistant_text(self, content: str) -> None:
        self._record(content)

    def final(self, content: str) -> None:
        self._record(content)

    def tool_start(self, tool_name: str, params: dict) -> None:
        self._record(f"→ {tool_name}({params})")

    def tool_succeeded(self, tool_name: str, result: ToolResult) -> None:
        self._record(f"✓ {tool_name}")

    def tool_failed(self, tool_name: str, result: ToolResult) -> None:
        self._record(f"✗ {tool_name}: {result.error}")

    def token_usage(self, usage: Optional[TokenUsage]) -> None:
        if usage:
            self._record(f"tokens: {usage.total_tokens}")


class ApprovalStrategy(ABC):
    """How a tool call is approved before execution."""

    @abstractmethod
    async def approve(self, tool: BaseTool, params: dict) -> ToolApprovalResult:
        pass


class InteractiveApproval(ApprovalStrategy):
    """Master strategy: defer to the CLI approval helper + current mode."""

    def __init__(
        self,
        helper: ToolApprovalCliHelper,
        mode_manager: ApprovalModeManager,
    ):
        self._helper = helper
        self._mode_manager = mode_manager

    async def approve(self, tool: BaseTool, params: dict) -> ToolApprovalResult:
        return await self._helper.get_tool_usage_approval(
            tool, params, self._mode_manager.current
        )


class WhitelistAutoApproval(ApprovalStrategy):
    """Agent strategy: a tool that reaches the loop is already whitelisted.

    An agent's tool list is the restricted set the user signed off on when
    approving the delegation, so anything the loop is asked to run is granted.
    """

    async def approve(self, tool: BaseTool, params: dict) -> ToolApprovalResult:
        return ToolApprovalResult(approved=True)


@dataclass
class LoopSession:
    """Everything `ConversationEngine.run_loop` needs for one turn.

    `history` is mutated in place — the caller's list reference stays valid
    across appends and compaction.
    """

    provider: BaseLLMProvider
    history: List[Message]
    tools: List[BaseTool]
    approval: ApprovalStrategy
    output: OutputSink
    max_iterations: int
    temperature: Optional[float] = None
    max_response_tokens: Optional[int] = None
    engagement_store: Optional[EngagementStore] = None
    knowledge_base: Optional[EngagementKnowledgeBase] = None
    kb_agent_type: Optional[str] = None
    kb_clip_threshold_bytes: Optional[int] = None
    compactor: Optional[ContextCompactor] = None
    token_tracker: Optional[TokenTracker] = None
    interrupt: Optional[InterruptHandle] = None
    transient_prompts: List[str] = field(default_factory=list)
    compaction_warning_shown: bool = False
    terminate_after_tool: Optional[TerminateAfterToolFn] = None


class ConversationEngine:
    """Runs the shared tool-execution loop against a `LoopSession`."""

    async def run_loop(self, session: LoopSession) -> Optional[LLMResponse]:
        """Drive the LLM/tool loop until a final response, cap, or interrupt.

        Returns the last `LLMResponse` seen, or `None` if the user interrupted.
        """
        session.output.start_turn()
        iteration = 0
        non_interactive_retry_used = False
        last_response: Optional[LLMResponse] = None

        try:
            while iteration < session.max_iterations:
                iteration += 1

                try:
                    response = await self._complete(session)
                except InterruptedByUser:
                    session.output.info("Interrupted by user")
                    if session.interrupt is not None:
                        session.interrupt.reset_interrupt()
                    return None

                last_response = response
                logger.info(f"Raw LLM response: {response}")

                if response.tool_calls:
                    should_terminate = await self._handle_tool_calls(session, response)
                    session.output.token_usage(response.usage)
                    self._record_usage(session, response.usage)
                    await self._check_compaction(session)
                    if should_terminate:
                        break
                    continue

                if response.content:
                    if (
                        session.transient_prompts
                        and not non_interactive_retry_used
                        and looks_like_deferred_execution(response.content)
                    ):
                        self._append(
                            session, Message(role="assistant", content=response.content)
                        )
                        self._append(
                            session,
                            Message(role="user", content=NON_INTERACTIVE_RETRY_PROMPT),
                        )
                        non_interactive_retry_used = True
                        self._record_usage(session, response.usage)
                        await self._check_compaction(session)
                        continue

                    self._append(
                        session, Message(role="assistant", content=response.content)
                    )
                    self._update_engagement_summary(session)
                    session.output.final(response.content)
                    session.output.token_usage(response.usage)
                    self._record_usage(session, response.usage)
                    await self._check_compaction(session)
                    session.output.info("")

                break

            if iteration >= session.max_iterations:
                session.output.warning(
                    f"⚠ Reached maximum iterations ({session.max_iterations}). "
                    "Stopping to prevent infinite loops."
                )

            return last_response
        finally:
            session.output.end_turn()

    async def _complete(self, session: LoopSession) -> LLMResponse:
        """Make one provider completion call, watching for interrupts."""
        max_response_tokens = await self._resolve_max_response_tokens(session)
        messages = self._messages_for_provider(session)
        with session.output.status(
            "[bold cyan]Thinking... [dim](Esc to interrupt)[/dim]"
        ):
            return await self._await_interruptible(
                session,
                session.provider.complete(
                    messages=messages,
                    temperature=session.temperature,
                    max_tokens=max_response_tokens,
                    tools=session.tools,
                ),
            )

    async def _handle_tool_calls(
        self, session: LoopSession, response: LLMResponse
    ) -> bool:
        """Execute every tool call in a turn, appending results to history.

        Returns True when `session.terminate_after_tool` signals the loop
        should stop after this turn (the typical agent path: stop right
        after `report` is called).
        """
        if response.content and response.content.strip():
            session.output.assistant_text(response.content)

        assert response.tool_calls is not None
        tool_names = [tc.tool_name for tc in response.tool_calls]
        session.output.info(
            f"→ Executing {len(response.tool_calls)} tool(s): {', '.join(tool_names)}"
        )

        self._append(
            session,
            Message(
                role="assistant",
                content=response.content,
                raw_blocks=extract_raw_blocks(response.raw_response),
            ),
        )

        denied = False
        interrupted = False
        should_terminate = False
        for tool_call in response.tool_calls:
            if denied or interrupted:
                reason = (
                    "Skipped due to prior tool denial."
                    if denied
                    else "Skipped — user interrupted a prior tool."
                )
                self._append(
                    session,
                    Message(role="tool", content=reason, call_id=tool_call.call_id),
                )
                continue

            session.output.tool_start(tool_call.tool_name, tool_call.parameters)

            tool = self._get_tool(session, tool_call.tool_name)
            if not tool:
                error_msg = f"Tool '{tool_call.tool_name}' not found"
                session.output.error(error_msg)
                self._append(
                    session,
                    Message(role="tool", content=error_msg, call_id=tool_call.call_id),
                )
                continue

            session.output.tool_pending_approval(
                tool_call.tool_name, tool_call.parameters
            )
            approval_result = await session.approval.approve(tool, tool_call.parameters)
            if not approval_result.approved:
                denial_msg = "User denied the tool call."
                if approval_result.user_message:
                    denial_msg += f" User said: {approval_result.user_message}"
                self._append(
                    session,
                    Message(role="tool", content=denial_msg, call_id=tool_call.call_id),
                )
                denied = True
                continue

            try:
                with session.output.status(
                    f"[bold yellow]Running {tool_call.tool_name}... "
                    "[dim](Esc to interrupt)[/dim]"
                ):
                    tool_result = await self._await_interruptible(
                        session,
                        tool.execute(
                            tool_call.parameters,
                            call_id=tool_call.call_id,
                        ),
                    )
            except InterruptedByUser:
                session.output.info(f"Interrupted: {tool_call.tool_name}")
                if session.interrupt is not None:
                    session.interrupt.reset_interrupt()
                self._append(
                    session,
                    Message(
                        role="tool",
                        content="Tool execution interrupted by user.",
                        call_id=tool_call.call_id,
                    ),
                )
                interrupted = True
                continue

            if tool_result.success:
                session.output.tool_succeeded(tool_call.tool_name, tool_result)
            else:
                session.output.tool_failed(tool_call.tool_name, tool_result)

            history_result = await self._ingest_and_clip(
                session, tool_call, tool_result
            )
            self._append(
                session,
                Message(
                    role="tool",
                    content=format_tool_result(tool_call.tool_name, history_result),
                    call_id=tool_call.call_id,
                ),
            )

            if (
                session.terminate_after_tool is not None
                and session.terminate_after_tool(tool_call, tool_result)
            ):
                should_terminate = True
                break

        return should_terminate

    async def _ingest_and_clip(
        self,
        session: LoopSession,
        tool_call: ToolCall,
        tool_result: ToolResult,
    ) -> ToolResult:
        """Index the tool output, and clip it out of history when it is large.

        Everything is indexed — a small result is cheap to store and just as
        worth recalling an hour later — but only output over the clip threshold
        is replaced by a reference, so cheap results are not hidden behind a
        recall round-trip.

        Returns the result to write into history: the original, or a copy whose
        output is the artifact reference.
        """
        if session.knowledge_base is None or not tool_result.output:
            return tool_result
        if tool_call.tool_name in KB_INGEST_SKIP_TOOLS:
            return tool_result

        engagement_id = None
        if session.engagement_store is not None:
            engagement_id = session.engagement_store.engagement_id
        engagement_id = engagement_id or session.knowledge_base.root_engagement_id

        try:
            artifact_id = await session.knowledge_base.add_artifact(
                engagement_id=engagement_id,
                body=tool_result.output,
                kind=artifact_kind_for_tool(tool_call.tool_name),
                source=tool_call.tool_name,
                title=artifact_title(tool_call),
                metadata={
                    "call_id": tool_call.call_id,
                    "success": tool_result.success,
                    "parameters": _summarize_parameters(tool_call.parameters),
                },
                agent_type=session.kb_agent_type,
            )
        except Exception as e:
            # Indexing is a bonus; never fail a tool call over it.
            logger.warning(f"Knowledge base ingestion failed: {e}")
            return tool_result

        threshold = session.kb_clip_threshold_bytes
        if threshold is None:
            return tool_result
        if len(tool_result.output.encode("utf-8")) <= threshold:
            return tool_result

        clipped = tool_result.model_copy(
            update={"output": build_clipped_output(tool_result.output, artifact_id)}
        )
        logger.info(f"Clipped {tool_call.tool_name} output into artifact#{artifact_id}")
        return clipped

    async def _await_interruptible(self, session: LoopSession, coro: Any) -> Any:
        """Await a coroutine, honoring the session's interrupt handle if present."""
        if session.interrupt is None:
            return await coro
        session.interrupt.start_watching_for_interrupt()
        try:
            return await run_cancellable(coro, session.interrupt.interrupt_requested)
        finally:
            session.interrupt.stop_watching_for_interrupt()

    async def _resolve_max_response_tokens(self, session: LoopSession) -> Optional[int]:
        """Pick the per-turn output cap: explicit config, else the model's cap."""
        if session.max_response_tokens is not None:
            return session.max_response_tokens
        return await session.provider.get_max_output_tokens()

    def _messages_for_provider(self, session: LoopSession) -> List[Message]:
        """Build the message list sent to the provider, with transient prompts."""
        messages = list(session.history)
        if session.transient_prompts:
            messages.extend(
                Message(role="system", content=prompt)
                for prompt in session.transient_prompts
            )
        return messages

    def _append(self, session: LoopSession, msg: Message) -> None:
        """Append a message to history and (if present) the engagement store."""
        session.history.append(msg)
        if session.engagement_store is not None:
            session.engagement_store.append_message(msg)

    def _get_tool(self, session: LoopSession, tool_name: str) -> Optional[BaseTool]:
        return next((t for t in session.tools if t.name == tool_name), None)

    def _record_usage(self, session: LoopSession, usage: Optional[TokenUsage]) -> None:
        """Replace the tracker total with authoritative numbers from usage."""
        if session.token_tracker is not None and usage and usage.prompt_tokens:
            session.token_tracker.record_usage(
                usage.prompt_tokens, usage.completion_tokens
            )

    def _update_engagement_summary(self, session: LoopSession) -> None:
        """Set the engagement summary to the first user message (truncated)."""
        if session.engagement_store is None:
            return
        for msg in session.history:
            if msg.role == "user":
                summary = msg.content[:80].replace("\n", " ")
                session.engagement_store.update_summary(summary)
                return

    async def _check_compaction(self, session: LoopSession) -> None:
        """Run context compaction if the token threshold is exceeded."""
        if session.compactor is None:
            return
        if not session.compactor.should_compact():
            self._warn_if_approaching_threshold(session)
            return

        updated_history = await session.compactor.compact(
            session.history,
            summarize_fn=self._make_summarize_fn(session),
        )
        if updated_history:
            if session.engagement_store is not None:
                session.engagement_store.override_engagement_messages(updated_history)
            session.history[:] = updated_history
            session.compaction_warning_shown = False
            tokens = (
                session.token_tracker.total_tokens
                if session.token_tracker is not None
                else 0
            )
            session.output.info(f"[context compacted: {tokens:,} tokens]")

    def _make_summarize_fn(
        self, session: LoopSession
    ) -> Callable[[str], Coroutine[Any, Any, str]]:
        """Build the focused-LLM-call summarizer the compactor invokes."""

        async def summarize(prompt: str) -> str:
            reserve = (
                session.compactor.summary_reserve
                if session.compactor is not None
                else None
            )
            with session.output.status("[bold orange]Compacting...[/bold orange]"):
                response = await session.provider.complete(
                    messages=[Message(role="user", content=prompt)],
                    temperature=0.0,
                    max_tokens=reserve,
                )
                return response.content

        return summarize

    def _warn_if_approaching_threshold(self, session: LoopSession) -> None:
        """Warn once when context usage approaches the compaction threshold."""
        if session.compactor is None:
            return
        usage_pct = session.compactor.usage_pct()
        if usage_pct >= 0.85 and not session.compaction_warning_shown:
            session.output.warning(
                f"Context at {usage_pct:.0%} capacity " "— compaction will trigger soon"
            )
            session.compaction_warning_shown = True
