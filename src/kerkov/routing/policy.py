"""Conservative, deterministic routing without model calls or application state."""

import re
from dataclasses import dataclass

# These rules select a cost tier, not an authorization boundary. Only the caller
# knows whether a profile is trusted and which tools the agent will actually get.
_CHEAP_TASK_KINDS = frozenset({"extract", "format"})
_CHEAP_TOOLS = frozenset({"read", "recall"})
_COMPLEX_TASK_LANGUAGE = re.compile(
    r"\b(?:exploit\w*|attack\w*|payload\w*|inject\w*|bypass\w*|"
    r"escalat\w*|exfiltrat\w*|credential\w*|password\w*|secret\w*|"
    r"authoriz\w*|authoris\w*|permission\w*|scope|vulnerab\w*|"
    r"risk\w*|severity|remediat\w*|execut\w*|command\w*|"
    r"analy[sz]\w*|assess\w*|interpret\w*|recommend\w*|prioriti[sz]\w*|"
    r"infer\w*|evaluat\w*|investigat\w*|diagnos\w*|decid\w*|reason\w*)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TaskRoutingContext:
    task_kind: str
    profile: str
    instruction: str
    success_criteria: str
    tools: tuple[str, ...]
    trusted_profile: bool


@dataclass(frozen=True)
class RoutingDecision:
    model: str
    tier: str
    reason: str


@dataclass(frozen=True)
class RoutingPolicy:
    cheap_model: str
    strong_model: str

    def __post_init__(self) -> None:
        for tier, model in (
            ("CHEAP", self.cheap_model),
            ("STRONG", self.strong_model),
        ):
            if not model or any(char.isspace() for char in model):
                raise ValueError(
                    f"AGENT_ROUTING_{tier}_MODEL must be a nonempty model ID "
                    "without whitespace."
                )
        if self.cheap_model == self.strong_model:
            raise ValueError("Agent routing requires two different model IDs.")

    def select(self, context: TaskRoutingContext) -> RoutingDecision:
        """Require every cheap-tier condition; uncertain work uses strong."""
        if context.task_kind not in _CHEAP_TASK_KINDS:
            return self._strong("Task is not explicitly eligible for cheap routing.")
        if context.profile != "analysis" or not context.trusted_profile:
            return self._strong("Task does not use the built-in analysis profile.")
        if not set(context.tools).issubset(_CHEAP_TOOLS):
            return self._strong("Effective tools exceed read and recall.")
        instruction = context.instruction.strip()
        criteria = context.success_criteria.strip()
        if (
            not instruction
            or not criteria
            or len(context.instruction) > 2000
            or len(context.success_criteria) > 1000
        ):
            return self._strong(
                "Task needs a bounded instruction and success criteria."
            )
        if _COMPLEX_TASK_LANGUAGE.search(f"{instruction}\n{criteria}"):
            return self._strong(
                "Task mentions reasoning, security decisions, or actions."
            )
        return RoutingDecision(
            self.cheap_model,
            "cheap",
            f"Explicit {context.task_kind} task with bounded criteria and read-only tools.",
        )

    def _strong(self, reason: str) -> RoutingDecision:
        return RoutingDecision(self.strong_model, "strong", reason)
