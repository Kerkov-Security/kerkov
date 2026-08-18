"""AgentRegistry — depth + total-agent budget tracking for the spawn tool.

Two independent failure modes, two independent caps:
- Depth: master → agent → agent. At the cap, the spawn tool is excluded
  from a nested agent's tool set, so it cannot delegate further.
- Total: hard ceiling on agents spawned in this process across the whole
  run. Catches wide fan-out the depth cap misses.

Per-agent iteration caps are not the registry's concern — they live on
the `AgentProfile` and are passed straight to the `LoopSession`.
"""

import threading
from dataclasses import dataclass


class BudgetExceeded(Exception):
    """Raised when the spawn tool would exceed the configured agent budget."""


@dataclass
class AgentRegistry:
    """Single-process counter for active and lifetime-spawned agents."""

    max_depth: int = 2
    max_total: int = 20

    def __post_init__(self) -> None:
        self._total_spawned: int = 0
        self._lock = threading.Lock()

    @property
    def total_spawned(self) -> int:
        return self._total_spawned

    def can_spawn(self, *, count: int, parent_depth: int) -> None:
        """Raise `BudgetExceeded` if `count` more agents would breach a cap.

        `parent_depth=0` is the master; an agent spawned by the master is
        depth 1; depth `max_depth` is the deepest level allowed to *be*,
        and anything spawned BY that agent would push past the cap.
        """
        with self._lock:
            if parent_depth >= self.max_depth:
                raise BudgetExceeded(
                    f"Depth cap reached: parent_depth={parent_depth} "
                    f">= max_depth={self.max_depth}"
                )
            if self._total_spawned + count > self.max_total:
                raise BudgetExceeded(
                    f"Total agent budget exhausted: "
                    f"{self._total_spawned} already spawned + {count} requested "
                    f"> max_total={self.max_total}"
                )

    def record(self, count: int) -> None:
        """Record `count` newly-spawned agents against the lifetime total."""
        with self._lock:
            self._total_spawned += count

    def depth_cap_reached(self, parent_depth: int) -> bool:
        """True when an agent at this depth must not be given the spawn tool."""
        return parent_depth >= self.max_depth
