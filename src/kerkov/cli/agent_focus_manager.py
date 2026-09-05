"""Which conversation the terminal is pointed at — the master, or one agent.

Focus is held by id (`a1`), never by name: a name is a display label the model
chose, and resolving it late would let a rename move focus somewhere else.
"""

from typing import Optional

from kerkov.core.logging.logger_factory import get_logger

logger = get_logger("AgentFocusManager")

MASTER_FOCUS = "master"


class AgentFocusManager:
    """Single source of truth for where typed input goes."""

    def __init__(self) -> None:
        self._agent_id: Optional[str] = None

    @property
    def current(self) -> str:
        """`"master"` or the focused agent's id."""
        return self._agent_id or MASTER_FOCUS

    @property
    def agent_id(self) -> Optional[str]:
        """The focused agent's id, or None while on the master."""
        return self._agent_id

    @property
    def is_master(self) -> bool:
        return self._agent_id is None

    def focus_master(self) -> None:
        if self._agent_id is not None:
            logger.info(f"Focus: {self._agent_id} -> master")
        self._agent_id = None

    def focus_agent(self, agent_id: str) -> None:
        logger.info(f"Focus: {self.current} -> {agent_id}")
        self._agent_id = agent_id
