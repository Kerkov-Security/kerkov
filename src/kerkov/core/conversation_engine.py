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
from pathlib import Path
from typing import (
    Any,
    Callable,
    ContextManager,
    Coroutine,
    Dict,
    FrozenSet,
    Iterator,
    List,
    Optional,
    Tuple,
)

from rich.console import Console, Group
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from kerkov.cli.approval_mode_manager import ApprovalModeManager
from kerkov.cli.approval_rules import render_call_string, split_write_content_lines
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
from kerkov.core.knowledge_base.findings_ledger import FindingsLedger
from kerkov.core.knowledge_base.models import ArtifactKind
from kerkov.core.logging.logger_factory import get_logger
from kerkov.core.tool_output import (  # noqa: F401  (re-exported)
    METADATA_VALUE_MAX_CHARS,
    TOOL_OUTPUT_HEAD_CHARS,
    TOOL_OUTPUT_MARKER,
    TOOL_OUTPUT_MAX_CHARS,
    TOOL_OUTPUT_TAIL_CHARS,
    build_clipped_metadata,
    build_clipped_output,
    format_bytes,
    normalize_tool_output,
)
from kerkov.models.basic_models import LLMResponse, Message, TokenUsage, ToolCall
from kerkov.providers.base_provider import BaseLLMProvider
from kerkov.tools.base_tool import BaseTool, ToolResult

TerminateAfterToolFn = Callable[[ToolCall, ToolResult], bool]

logger = get_logger("ConversationEngine")

KB_INGEST_SKIP_TOOLS = frozenset({"recall"})
KB_CLIP_EXEMPT_TOOLS = frozenset({"read_url"})
_TITLE_PARAM_KEYS = ("command", "url", "file_path", "path", "query", "action")
_TOOL_ARTIFACT_KINDS = {
    "read": ArtifactKind.FILE,
    "http": ArtifactKind.HTTP_RESPONSE,
}
WRITE_PREVIEW_MAX_LINES = 30


def _recalled_artifact_ref(tool_result: ToolResult) -> Optional[int]:
    """The artifact a skip-ingest result read back, when it read exactly one.

    A recalled body is already stored, so re-ingesting it would only duplicate
    the row — but the result *is* that stored artifact verbatim, and leaving the
    history message unstamped costs it the one thing the stamp buys: compaction
    evicts an unstamped tool result to an unrecoverable head/tail clip, where a
    stamped one collapses back to the handle it came from.
    """
    metadata = tool_result.metadata or {}
    artifact_id = metadata.get("artifact_id")
    if isinstance(artifact_id, int):
        return artifact_id

    hits = metadata.get("hits")
    if isinstance(hits, list) and len(hits) == 1 and isinstance(hits[0], dict):
        single = hits[0].get("artifact_id")
        if isinstance(single, int):
            return single
    return None


def _format_seconds(s: float) -> str:
    total = int(s)
    if total < 60:
        return f"{total}s"
    return f"{total // 60}m{total % 60}s"


def _write_preview_lexer(file_path: str, content: str) -> str:
    if Path(file_path).suffix.lower() == ".nse":
        return "lua"
    lexer = Syntax.guess_lexer(file_path, content)
    if lexer != "default":
        return lexer

    first_line = content.partition("\n")[0]
    if first_line.startswith("#!"):
        for name, shebang_lexer in (
            ("python", "python"),
            ("bash", "bash"),
            ("zsh", "zsh"),
            ("sh", "bash"),
            ("ruby", "ruby"),
            ("perl", "perl"),
        ):
            if name in first_line:
                return shebang_lexer

    return "text"


def _escape_write_preview_controls(value: str) -> str:
    """Make display-altering controls visible while preserving code indentation."""
    return "".join(
        (
            character
            if character == "\t" or character.isprintable()
            else ascii(character)[1:-1]
        )
        for character in value
    )


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

OPERATOR_MESSAGE_PREFIX = "Message from the operator watching this run:\n"


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
    ]

    if tool_result.error:
        result_parts.append(f"Error: {tool_result.error}")

    if tool_result.metadata:
        result_parts.append(f"Metadata: {tool_result.metadata}")

    # Output goes last so `split_tool_output` can lift it off at compaction
    # without parsing past a body that may contain anything.
    result_parts.append(f"{TOOL_OUTPUT_MARKER}{tool_result.output}")

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


def history_footprint_bytes(tool_result: ToolResult) -> int:
    """Size a result will occupy in history — output and metadata together."""
    size = len(tool_result.output.encode("utf-8"))
    if tool_result.metadata:
        size += len(repr(tool_result.metadata).encode("utf-8"))
    return size


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

    @property
    def supports_streaming(self) -> bool:
        """Whether this sink renders assistant text token-by-token.

        The engine only threads a delta callback into the provider when this is
        True. When False (for example, agents) the provider accumulates the
        response internally and returns it whole — the cheap, unchanged path.
        """
        return False

    def begin_assistant_stream(self) -> None:
        """A streamed assistant turn is starting. Default: no-op."""

    def assistant_delta(self, text: str) -> None:
        """One chunk of streamed assistant text. Default: no-op."""

    def end_assistant_stream(self) -> None:
        """The streamed assistant turn has ended. Default: no-op."""

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

    def iteration_start(self, n: int) -> None:
        """A new loop iteration is beginning (1-based)."""
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

    def __init__(self, console: Console, stream: bool = True) -> None:
        self._console = console
        self._stream = stream
        self._timer = TurnTimer()
        self._active_status: Optional[Any] = None
        self._streamed_content = ""
        self._stream_open = False

    @property
    def supports_streaming(self) -> bool:
        return self._stream

    def begin_assistant_stream(self) -> None:
        # The leading separator and spinner stop are deferred to the first
        # actual delta, so a stream that yields nothing leaves no stray blank.
        self._streamed_content = ""
        self._stream_open = False

    def assistant_delta(self, text: str) -> None:
        if not text:
            return
        if not self._stream_open:
            if self._active_status is not None:
                self._active_status.stop()
            self._console.print()
            self._stream_open = True
        self._streamed_content += text
        # emoji=False keeps `:a:`-style substrings (e.g. inside an IPv6 URL)
        # from being rewritten as emoji shortcodes in raw streamed text.
        self._console.print(
            text,
            end="",
            markup=False,
            highlight=False,
            emoji=False,
            soft_wrap=True,
        )

    def end_assistant_stream(self) -> None:
        if self._stream_open:
            self._console.print()
            self._stream_open = False

    def _consume_streamed(self, content: str) -> bool:
        """True when `content` was already streamed live, so skip re-rendering.

        Avoids double-printing the completed Markdown on top of the raw text the
        user already watched arrive. A mismatch (e.g. the stream was interrupted
        by an attach mid-way) falls through and renders normally, so nothing is
        lost.
        """
        streamed = self._streamed_content
        if streamed and content.strip() == streamed.strip():
            self._streamed_content = ""
            return True
        return False

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
        if self._consume_streamed(content):
            return
        self._console.print()
        self._console.print(Markdown(content))
        self._console.print()

    def final(self, content: str) -> None:
        if self._consume_streamed(content):
            return
        self._console.print()
        self._console.print(Markdown(content))
        self._console.print()

    def tool_start(self, tool_name: str, params: dict) -> None:
        if tool_name == "write":
            self._render_write_start(params)
            return

        call = escape(f"{tool_name}({params})")
        self._console.print(f"[dim]→ Executing: {call}[/dim]")

    def _render_write_start(self, params: dict) -> None:
        file_path = str(params.get("file_path", "<unknown>"))
        content = str(params.get("content", ""))
        lines = split_write_content_lines(content)

        call = escape(render_call_string("write", params))
        self._console.print(f"[dim]→ Executing: {call}[/dim]")
        if not lines:
            return

        visible_lines = lines[:WRITE_PREVIEW_MAX_LINES]
        preview = "\n".join(
            _escape_write_preview_controls(line) for line in visible_lines
        )
        omitted_lines = len(lines) - len(visible_lines)

        renderables: List[Any] = [
            Syntax(
                preview,
                _write_preview_lexer(file_path, content),
                line_numbers=True,
                word_wrap=False,
                background_color="default",
            )
        ]
        if omitted_lines:
            renderables.append(
                Text(f"… +{omitted_lines} more lines", style="dim italic")
            )
        self._console.print(
            Panel(Group(*renderables), border_style="dim", padding=(0, 1))
        )

    def tool_succeeded(self, tool_name: str, result: ToolResult) -> None:
        self._console.print("[dim green]✓ Command executed[/dim green]")
        if result.output:
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
        call = (
            render_call_string(tool_name, params)
            if tool_name == "write"
            else f"{tool_name}({params})"
        )
        self._record(f"→ {call}")

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
    tool_output_max_chars: int = TOOL_OUTPUT_MAX_CHARS
    tool_output_head_chars: int = TOOL_OUTPUT_HEAD_CHARS
    tool_output_tail_chars: int = TOOL_OUTPUT_TAIL_CHARS
    compactor: Optional[ContextCompactor] = None
    token_tracker: Optional[TokenTracker] = None
    interrupt: Optional[InterruptHandle] = None
    # Agents and the web Stop control end the turn on an interrupt. The CLI's
    # Escape means "stop this tool", so it keeps the default behavior.
    interrupt_ends_turn: bool = False
    # Operator text queued for this run, drained at the top of each iteration.
    inbox: Optional["asyncio.Queue[str]"] = None
    # Tools that still run once a stop is requested. `report` writes nothing
    # outside the run and is how a stopped agent hands back what it found, so
    # skipping it would throw away the findings the stop was meant to preserve.
    stop_exempt_tools: FrozenSet[str] = frozenset()
    transient_prompts: List[str] = field(default_factory=list)
    compaction_warning_shown: bool = False
    compaction_stalled_warning_shown: bool = False
    terminate_after_tool: Optional[TerminateAfterToolFn] = None
    # `ledger` is the source of truth; `ledger_block` is its serialized string,
    # refreshed each iteration so `_messages_for_provider` never renders a stale
    # copy. The block is transient — never appended to `history` — so it stays
    # outside the compactable range.
    ledger: Optional[FindingsLedger] = None
    ledger_block: Optional[str] = None


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
                if self._stop_requested(session):
                    # A cancel that landed between awaits raises nowhere; this
                    # is the only place it can be noticed.
                    session.output.info("Stopping at the operator's request")
                    break

                iteration += 1
                session.output.iteration_start(iteration)

                self._drain_inbox(session)
                await self._refresh_ledger_block(session)

                try:
                    response = await self._complete(session)
                except InterruptedByUser as e:
                    partial = getattr(e, "partial_text", "") or ""
                    if partial.strip():
                        # Text the user already watched stream in must not vanish
                        # from history — record it as an explicitly partial turn.
                        self._append(
                            session,
                            Message(role="assistant", content=partial, partial=True),
                        )
                    session.output.info("Interrupted by user")
                    if session.interrupt_ends_turn:
                        # Leave the handle set: the caller reads it to tell a
                        # cancelled run from one that simply finished.
                        break
                    if session.interrupt is not None:
                        session.interrupt.reset_interrupt()
                    return None

                last_response = response
                logger.info(f"Raw LLM response: {response}")

                if response.tool_calls:
                    should_terminate = await self._handle_tool_calls(session, response)
                    session.output.token_usage(response.usage)
                    self._record_usage(session, response.usage)
                    if should_terminate:
                        # A terminating tool (an agent's `report`) has already
                        # produced the run's result. Compacting now risks a
                        # summarizer failure raising out of the loop, losing that
                        # result to the supervisor's `stuck` stub — and there is
                        # no next turn for a reclaimed context to serve anyway.
                        break
                    await self._check_compaction(session)
                    continue

                if response.content:
                    if (
                        session.transient_prompts
                        and not non_interactive_retry_used
                        and looks_like_deferred_execution(response.content)
                    ):
                        self._append(
                            session,
                            Message(
                                role="assistant",
                                content=response.content,
                                raw_blocks=extract_raw_blocks(response.raw_response),
                            ),
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
                        session,
                        Message(
                            role="assistant",
                            content=response.content,
                            raw_blocks=extract_raw_blocks(response.raw_response),
                        ),
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
        """Make one provider completion call, watching for interrupts.

        When the sink streams, a delta callback is threaded into the provider so
        assistant text renders as it arrives; the chunks are also accumulated so
        an interrupt mid-generation can surface what was already shown as
        `InterruptedByUser.partial_text` instead of losing it.
        """
        if self._stop_requested(session):
            raise InterruptedByUser()
        if session.interrupt_ends_turn:
            max_response_tokens = await self._await_interruptible(
                session, self._resolve_max_response_tokens(session)
            )
        else:
            max_response_tokens = await self._resolve_max_response_tokens(session)
        if self._stop_requested(session):
            raise InterruptedByUser()
        messages = self._messages_for_provider(session)

        streamed_parts: List[str] = []
        on_delta: Optional[Callable[[str], None]] = None
        if session.output.supports_streaming:

            def on_delta(text: str) -> None:
                streamed_parts.append(text)
                session.output.assistant_delta(text)

            session.output.begin_assistant_stream()

        async def complete() -> LLMResponse:
            if self._stop_requested(session):
                raise InterruptedByUser()
            return await session.provider.complete(
                messages=messages,
                temperature=session.temperature,
                max_tokens=max_response_tokens,
                tools=session.tools,
                on_delta=on_delta,
            )

        try:
            with session.output.status(
                "[bold cyan]Thinking... [dim](Esc to interrupt)[/dim]"
            ):
                response = await self._await_interruptible(session, complete())
                # Completion can beat the polling watcher after Stop is set.
                # Keep any streamed text partial instead of committing this
                # late response as a completed answer or a new tool batch.
                if self._stop_requested(session):
                    raise InterruptedByUser()
                return response
        except InterruptedByUser as e:
            if streamed_parts:
                e.partial_text = "".join(streamed_parts)
            raise
        finally:
            if on_delta is not None:
                session.output.end_assistant_stream()

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
        stopped = False
        should_terminate = False
        for tool_call in response.tool_calls:
            if not stopped and self._stop_requested(session):
                # A stop that lands between calls must not start the next one:
                # `run_cancellable` schedules the coroutine anyway, and a tool
                # that finishes without yielding beats the interrupt watcher
                # and lands its side effect regardless.
                stopped = True
                should_terminate = True

            skip_reason = self._skip_reason(
                session, tool_call, denied, interrupted, stopped
            )
            if skip_reason is not None:
                self._append(
                    session,
                    Message(
                        role="tool", content=skip_reason, call_id=tool_call.call_id
                    ),
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
            try:
                stop_sensitive = (
                    session.interrupt_ends_turn
                    and tool_call.tool_name not in session.stop_exempt_tools
                )
                if stop_sensitive:
                    approval_result = await self._await_interruptible(
                        session, session.approval.approve(tool, tool_call.parameters)
                    )
                else:
                    approval_result = await session.approval.approve(
                        tool, tool_call.parameters
                    )
                # Approval and Stop may arrive together. A completed approval
                # must never start an instant side effect after Stop won.
                if stop_sensitive and self._stop_requested(session):
                    raise InterruptedByUser()
                if not approval_result.approved:
                    denial_msg = "User denied the tool call."
                    if approval_result.user_message:
                        denial_msg += f" User said: {approval_result.user_message}"
                    self._append(
                        session,
                        Message(
                            role="tool", content=denial_msg, call_id=tool_call.call_id
                        ),
                    )
                    denied = True
                    continue

                with session.output.status(
                    f"[bold yellow]Running {tool_call.tool_name}... "
                    "[dim](Esc to interrupt)[/dim]"
                ):
                    tool_result = await self._await_interruptible(
                        session, self._execute_tool(session, tool, tool_call)
                    )
            except InterruptedByUser:
                session.output.info(f"Interrupted: {tool_call.tool_name}")
                if session.interrupt_ends_turn:
                    # Leave the handle set so the caller can tell why the loop
                    # ended, and stop after the remaining calls are answered.
                    should_terminate = True
                elif session.interrupt is not None:
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

            history_result, artifact_id = await self._ingest_and_clip(
                session, tool_call, tool_result
            )
            self._append(
                session,
                Message(
                    role="tool",
                    content=format_tool_result(tool_call.tool_name, history_result),
                    call_id=tool_call.call_id,
                    artifact_ref=artifact_id,
                ),
            )

            if (
                session.terminate_after_tool is not None
                and session.terminate_after_tool(tool_call, tool_result)
            ):
                should_terminate = True
                break

        return should_terminate or self._stop_requested(session)

    async def _execute_tool(
        self, session: LoopSession, tool: BaseTool, tool_call: ToolCall
    ) -> ToolResult:
        # Check inside the scheduled coroutine too: Stop can arrive after
        # approval but before an instant tool gets its first event-loop turn.
        if (
            self._stop_requested(session)
            and tool_call.tool_name not in session.stop_exempt_tools
        ):
            raise InterruptedByUser()
        return await tool.execute(tool_call.parameters, call_id=tool_call.call_id)

    @staticmethod
    def _skip_reason(
        session: LoopSession,
        tool_call: ToolCall,
        denied: bool,
        interrupted: bool,
        stopped: bool,
    ) -> Optional[str]:
        """Why this call is answered without running it, if it is.

        Every `tool_use` still needs a `tool_result`, so a skipped call is
        answered with the reason rather than dropped.
        """
        if denied:
            return "Skipped due to prior tool denial."
        if interrupted:
            return "Skipped — user interrupted a prior tool."
        if stopped and tool_call.tool_name not in session.stop_exempt_tools:
            return "Skipped — the run was stopped at the operator's request."
        return None

    async def _ingest_and_clip(
        self,
        session: LoopSession,
        tool_call: ToolCall,
        tool_result: ToolResult,
    ) -> Tuple[ToolResult, Optional[int]]:
        """Store raw output, then normalize only the copy entering history.

        Normalization works independently of the KB and its byte threshold.
        Any stored output that normalization changes gets a recall handle, even
        below that threshold. The original result remains available to sinks
        and termination callbacks. Recall itself is exempt: clipping an explicit
        full-artifact expansion would make the omitted middle inaccessible.
        Read URL and ledger graph pages keep their own budgets and boundaries.
        """
        if tool_call.tool_name in KB_INGEST_SKIP_TOOLS:
            return tool_result, _recalled_artifact_ref(tool_result)

        artifact_id = await self._ingest_tool_output(session, tool_call, tool_result)
        # Keep the requested page visible; any second preview hides text before
        # next_offset, including when the KB is disabled or ingestion fails.
        if tool_call.tool_name in KB_CLIP_EXEMPT_TOOLS or (
            tool_call.tool_name == "ledger"
            and (tool_call.parameters or {}).get("op") == "graph"
            and tool_result.success
        ):
            return tool_result, artifact_id

        normalized, meta = normalize_tool_output(
            tool_result.output,
            max_chars=session.tool_output_max_chars,
            head=session.tool_output_head_chars,
            tail=session.tool_output_tail_chars,
        )
        kb_clip = (
            artifact_id is not None
            and session.kb_clip_threshold_bytes is not None
            and history_footprint_bytes(tool_result) > session.kb_clip_threshold_bytes
        )
        # HTTP results may repeat the entire body in parsed metadata. Eliding
        # only output would let that second copy leak straight into history,
        # including when the KB is off or its write failed.
        metadata = tool_result.metadata
        metadata_chars = len(repr(metadata)) if metadata else 0
        if (
            kb_clip
            or len(tool_result.output) + metadata_chars > session.tool_output_max_chars
        ):
            metadata = build_clipped_metadata(metadata, artifact_id)

        if artifact_id is not None and (kb_clip or meta.was_clipped):
            normalized = build_clipped_output(
                tool_result.output, artifact_id, normalized_preview=normalized
            )
        if normalized == tool_result.output and metadata == tool_result.metadata:
            return tool_result, artifact_id
        return (
            tool_result.model_copy(update={"output": normalized, "metadata": metadata}),
            artifact_id,
        )

    async def _ingest_tool_output(
        self,
        session: LoopSession,
        tool_call: ToolCall,
        tool_result: ToolResult,
    ) -> Optional[int]:
        """Index the unmodified body; storage failure must not bypass clipping."""
        if session.knowledge_base is None or not tool_result.output:
            return None

        engagement_id = None
        if session.engagement_store is not None:
            engagement_id = session.engagement_store.engagement_id
        engagement_id = engagement_id or session.knowledge_base.root_engagement_id

        try:
            return await session.knowledge_base.add_artifact(
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
            return None

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
        if self._stop_requested(session):
            raise InterruptedByUser()
        if session.max_response_tokens is not None:
            return session.max_response_tokens
        return await session.provider.get_max_output_tokens()

    def _stop_requested(self, session: LoopSession) -> bool:
        """True when this run should end because its interrupt is set."""
        return (
            session.interrupt_ends_turn
            and session.interrupt is not None
            and session.interrupt.interrupt_requested.is_set()
        )

    def _drain_inbox(self, session: LoopSession) -> None:
        """Take whatever the operator queued since the last iteration.

        The iteration top is the only point where history is provider-valid for
        an insert: every `tool_use` already has its result, so no provider sees
        a user message wedged between a call and its result.
        """
        if session.inbox is None:
            return
        while True:
            try:
                text = session.inbox.get_nowait()
            except asyncio.QueueEmpty:
                return
            self._append(
                session,
                Message(role="user", content=OPERATOR_MESSAGE_PREFIX + text),
            )

    async def _refresh_ledger_block(self, session: LoopSession) -> None:
        """Re-serialize the ledger for this iteration (cheap — memoized on revision).

        Runs per iteration, not per call, so a fact recorded earlier in the same
        turn is reflected in the next iteration's injected block.
        """
        if session.ledger is None:
            return
        try:
            session.ledger_block = await session.ledger.serialize() or None
        except Exception as e:
            logger.warning(f"Could not serialize findings ledger: {e}")

    def _messages_for_provider(self, session: LoopSession) -> List[Message]:
        """Build the message list sent to the provider.

        Placement of the mutable ledger block depends on the provider. Providers
        that cache a stable prompt prefix (`caches_prompt_prefix`, currently only
        Anthropic) get the ledger at the *tail*, after the conversation, so it
        stays out of the cached prefix and only the small ledger tail is re-sent
        uncached each turn. This trades the ledger's head-position salience for
        recency-position salience — a deliberate change from the historical
        "high-priority working state before the history" placement, made to keep
        the cache prefix stable. Every other provider keeps the ledger high (just
        under the base system prompt), where it is most salient and where a
        trailing system block would risk a consecutive same-role turn.

        Transient prompts (non-interactive nudge, verbosity) always append at the
        end. Neither the ledger nor transient prompts are written into `history`.
        """
        messages = list(session.history)
        tail_ledger = session.provider.caches_prompt_prefix
        if session.ledger_block and not tail_ledger:
            insert_at = 1 if messages and messages[0].role == "system" else 0
            messages.insert(
                insert_at, Message(role="system", content=session.ledger_block)
            )
        if session.transient_prompts:
            messages.extend(
                Message(role="system", content=prompt)
                for prompt in session.transient_prompts
            )
        if session.ledger_block and tail_ledger:
            messages.append(Message(role="system", content=session.ledger_block))
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
        if self._stop_requested(session):
            return
        if session.compactor is None:
            return
        if not session.compactor.should_compact():
            self._warn_if_approaching_threshold(session)
            return

        compaction = session.compactor.compact(
            session.history, summarize_fn=self._make_summarize_fn(session)
        )
        if session.interrupt_ends_turn:
            try:
                updated_history = await self._await_interruptible(session, compaction)
            except InterruptedByUser:
                session.output.info(
                    "Stopped context compaction at the operator's request"
                )
                return
        else:
            updated_history = await compaction
        if self._stop_requested(session):
            return
        if updated_history is None:
            self._warn_compaction_stalled(session)
            return

        if session.engagement_store is not None:
            session.engagement_store.override_engagement_messages(updated_history)
        session.history[:] = updated_history
        session.compaction_warning_shown = False
        session.compaction_stalled_warning_shown = False
        tokens = (
            session.token_tracker.total_tokens
            if session.token_tracker is not None
            else 0
        )
        session.output.info(f"[context compacted: {tokens:,} tokens]")

    def _warn_compaction_stalled(self, session: LoopSession) -> None:
        """Surface a compaction that reclaimed nothing while over the threshold.

        Without this the engine asks for compaction, gets nothing back and says
        nothing, every turn — the state that most needs reporting is the one
        that used to be silent. Warned once per stall, reset by the next
        compaction that succeeds.
        """
        if session.compaction_stalled_warning_shown:
            return

        session.compaction_stalled_warning_shown = True
        session.output.warning(
            "Context is over the compaction threshold but nothing could be "
            "compacted — recent messages are filling the window"
        )

    def _make_summarize_fn(
        self, session: LoopSession
    ) -> Callable[[str], Coroutine[Any, Any, str]]:
        """Build the focused-LLM-call summarizer the compactor invokes."""

        async def summarize(prompt: str) -> str:
            if self._stop_requested(session):
                raise InterruptedByUser()
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
                # Compaction is billed too. It is not part of context size, so
                # it goes to cumulative spend only — replacing `total_tokens`
                # with a summarizer's prompt would misreport the conversation.
                if session.token_tracker is not None and response.usage:
                    session.token_tracker.record_side_spend(
                        response.usage.prompt_tokens, response.usage.completion_tokens
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
