"""Browser controls for the current engagement's supervised agents."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from kerkov.agents.agent_session import AgentSession
from kerkov.agents.agent_supervisor import AgentSupervisor
from kerkov.core.process.ansi_strip import strip_ansi

if TYPE_CHECKING:
    from kerkov.web.runtime.session import WebKerkovSession

REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
AGENT_ID_PATTERN = re.compile(r"a[1-9][0-9]{0,15}\Z")
AGENT_RUN_ID_PATTERN = re.compile(r"[0-9a-f]{12}\Z")
AGENT_REQUESTS = frozenset({"agent.inspect", "agent.message", "agent.stop"})
MAX_INSTRUCTION_CHARS = 8_000
MAX_PENDING_INSTRUCTIONS = 8
MAX_CONTROL_MUTATIONS = 4096
MAX_OUTPUT_CHARS = 32_000
MAX_ACTIVITY_CHARS = 8_000


class AgentControlError(ValueError):
    """A literal failure safe to display in the browser."""


def _valid_id(value: Any, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _bounded_tail(parts: Iterable[str], limit: int) -> tuple[str, bool]:
    """Read newest-first entries without joining an unbounded transcript."""
    kept: list[str] = []
    remaining = limit
    truncated = False
    for part in parts:
        separator = 2 if kept else 0
        available = remaining - separator
        if available <= 0:
            truncated = True
            break
        if len(part) > available:
            kept.append(part[-available:])
            truncated = True
            break
        kept.append(part)
        remaining -= len(part) + separator
    return strip_ansi("\n\n".join(reversed(kept))), truncated


class WebAgents:
    """Adapt supervisor operations without taking the master conversation lock."""

    def __init__(self, session: WebKerkovSession) -> None:
        self.session = session
        # Keep accepted mutations for the connection lifetime. Eviction would let
        # a delayed retry enqueue the same instruction again after it was read.
        self._mutation_ids: set[str] = set()

    @staticmethod
    def request_identity(content: dict[str, Any]) -> dict[str, str]:
        """Echo only validated opaque IDs, never arbitrary request content."""
        return {
            key: value if _valid_id(value := content.get(key), pattern) else ""
            for key, pattern in (
                ("request_id", REQUEST_ID_PATTERN),
                ("agent_id", AGENT_ID_PATTERN),
                ("agent_run_id", AGENT_RUN_ID_PATTERN),
            )
        }

    def reject(self, content: dict[str, Any], detail: str) -> None:
        if self.session._closed:
            return
        event_type = content.get("type")
        operation = (
            event_type.removeprefix("agent.")
            if isinstance(event_type, str) and event_type in AGENT_REQUESTS
            else ""
        )
        self.session.bus.emit(
            "agent.control_error",
            {
                **self.request_identity(content),
                "operation": operation,
                "detail": detail,
            },
        )

    def _current(self, supervisor: AgentSupervisor, agent: AgentSession) -> bool:
        return (
            not self.session._closed
            and self.session._agent_events_enabled
            and supervisor is self.session.agent_supervisor
            and supervisor.get(agent.id) is agent
        )

    async def handle(self, content: dict[str, Any]) -> None:
        if self.session._closed:
            return
        supervisor = self.session.agent_supervisor
        agent = None
        try:
            identity = self.request_identity(content)
            if not all(identity.values()):
                raise AgentControlError("Invalid agent control request IDs.")
            if content.get("type") not in AGENT_REQUESTS:
                raise AgentControlError("Unsupported agent operation.")
            agent = supervisor.get(identity["agent_id"])
            if (
                agent is None
                or agent.agent_run_id != identity["agent_run_id"]
                or not self._current(supervisor, agent)
            ):
                raise AgentControlError(
                    "That agent is no longer available. Refresh the agent panel."
                )
            if content["type"] == "agent.inspect":
                self._inspect(agent, identity)
                return
            if (
                not agent.is_active
                or agent.cancel_requested
                or (agent.task_handle is not None and agent.task_handle.done())
            ):
                raise AgentControlError("This agent has finished or is stopping.")
            if identity["request_id"] in self._mutation_ids:
                raise AgentControlError("This agent request was already accepted.")
            if len(self._mutation_ids) >= MAX_CONTROL_MUTATIONS:
                raise AgentControlError(
                    "The agent control request limit was reached. Open a new session."
                )
            if content["type"] == "agent.message":
                self._message(agent, identity, content.get("message"))
                return
            if agent.task_handle is None or agent.task_handle.done():
                raise AgentControlError("This agent has already stopped.")
            self._mutation_ids.add(identity["request_id"])
            agent.cancel_requested = True
            agent.notify("state")
            await supervisor.cancel(agent.id)
            if self._current(supervisor, agent):
                self.session.bus.emit(
                    "agent.stopped", {**identity, "detail": "Agent stopped."}
                )
        except AgentControlError as exc:
            self.reject(content, str(exc))
        except Exception:
            if agent is None or self._current(supervisor, agent):
                self.reject(content, "The agent request failed. Please retry.")

    def _inspect(self, agent: AgentSession, identity: dict[str, str]) -> None:
        output, output_truncated = _bounded_tail(
            (
                f"{message.role.capitalize()}:\n{message.content}"
                for message in reversed(agent.history)
                if message.role in {"user", "assistant", "tool"}
            ),
            MAX_OUTPUT_CHARS,
        )
        activity, activity_truncated = _bounded_tail(
            reversed(agent.events.buffer), MAX_ACTIVITY_CHARS
        )
        self.session.bus.emit(
            "agent.output",
            {
                **identity,
                "output": output,
                "activity": activity,
                "truncated": output_truncated or activity_truncated,
            },
        )

    def _message(
        self, agent: AgentSession, identity: dict[str, str], message: Any
    ) -> None:
        if not isinstance(message, str) or not message.strip():
            raise AgentControlError("Instructions cannot be empty.")
        if len(message) > MAX_INSTRUCTION_CHARS:
            raise AgentControlError(
                f"Instructions exceed the {MAX_INSTRUCTION_CHARS:,}-character limit."
            )
        if agent.inbox.qsize() >= MAX_PENDING_INSTRUCTIONS:
            raise AgentControlError(
                "This agent has too many queued instructions. Wait for it to read them."
            )
        self._mutation_ids.add(identity["request_id"])
        text = message.strip()
        agent.inbox.put_nowait(text)
        agent.events.info(f"> {text}")
        self.session.bus.emit(
            "agent.message_sent",
            {
                **identity,
                "detail": (
                    "Queued for the next agent step. The agent may finish "
                    "before reading it."
                ),
            },
        )
