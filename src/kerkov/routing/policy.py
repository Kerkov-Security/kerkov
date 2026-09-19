"""Conservative, deterministic routing without model calls or application state."""

import re
from dataclasses import dataclass

# These rules select a cost tier, not an authorization boundary. Only the caller
# knows whether a profile is trusted and which tools the agent will actually get.
_SMALL_TASK_KINDS = frozenset({"extract", "format"})
_RESTRICTED_TOOLS = frozenset({"read", "recall"})
_SECURITY_OR_ACTION_LANGUAGE = re.compile(
    r"\b(?:exploit\w*|attack\w*|payload\w*|inject\w*|bypass\w*|"
    r"escalat\w*|exfiltrat\w*|credential\w*|password\w*|secret\w*|"
    r"authoriz\w*|authoris\w*|permission\w*|scope|vulnerab\w*|"
    r"risk\w*|severity|remediat\w*|execut\w*|command\w*|"
    r"deploy\w*|delet\w*|modif\w*|install\w*|scan\w*|pentest\w*|"
    r"cve|cwe|xss|sqli|ssrf|rce|xxe|csrf|idor|authenticat\w*|"
    r"shell\w*|root[ -]access|privileg\w*|security|threat\w*|malware|phish\w*)\b",
    re.IGNORECASE,
)
_COMPLEX_REASONING_LANGUAGE = re.compile(
    r"\b(?:complex\w*|ambigu\w*|architect\w*|cryptograph\w*|"
    r"algorithm\w*|optimi[sz]\w*|theorem\w*|proof\w*|prov(?:e|ing)|"
    r"multi[ -]?step|cross[ -]?system|root[ -]?cause|causal\w*)\b",
    re.IGNORECASE,
)
_ROUTINE_REASONING_LANGUAGE = re.compile(
    r"\b(?:"
    r"analy[sz]\w*|assess\w*|interpret\w*|recommend\w*|prioriti[sz]\w*|"
    r"infer\w*|evaluat\w*|investigat\w*|diagnos\w*|decid\w*|reason\w*|"
    r"compar\w*|explain\w*|synthesi[sz]\w*)\b",
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
    small_model: str
    medium_model: str
    large_model: str

    def __post_init__(self) -> None:
        for tier, model in (
            ("SMALL", self.small_model),
            ("MEDIUM", self.medium_model),
            ("LARGE", self.large_model),
        ):
            if not model or any(char.isspace() for char in model):
                raise ValueError(
                    f"AGENT_ROUTING_{tier}_MODEL must be a nonempty model ID "
                    "without whitespace."
                )
        if len({self.small_model, self.medium_model, self.large_model}) != 3:
            raise ValueError("Agent routing requires three different model IDs.")

    def select(self, context: TaskRoutingContext) -> RoutingDecision:
        """Require every lower-tier condition; uncertain work uses large.

        Language screening is a conservative heuristic, not a complexity grader.
        """
        if context.task_kind not in _SMALL_TASK_KINDS | {"analyze"}:
            return self._large(
                "Task is not explicitly eligible for small or medium routing."
            )
        if context.profile != "analysis" or not context.trusted_profile:
            return self._large("Task does not use the built-in analysis profile.")
        if not set(context.tools).issubset(_RESTRICTED_TOOLS):
            return self._large("Effective tools exceed read and recall.")
        instruction = context.instruction.strip()
        criteria = context.success_criteria.strip()
        instruction_limit = 4000 if context.task_kind == "analyze" else 2000
        if (
            not instruction
            or not criteria
            or len(context.instruction) > instruction_limit
            or len(context.success_criteria) > 1000
        ):
            return self._large("Task needs a bounded instruction and success criteria.")
        task_text = f"{instruction}\n{criteria}"
        if _SECURITY_OR_ACTION_LANGUAGE.search(task_text):
            return self._large("Task mentions security decisions or actions.")
        if _COMPLEX_REASONING_LANGUAGE.search(task_text):
            return self._large("Task mentions complex reasoning.")
        if context.task_kind == "analyze":
            return RoutingDecision(
                self.medium_model,
                "medium",
                "Explicit analyze task with bounded criteria and read-only tools.",
            )
        if _ROUTINE_REASONING_LANGUAGE.search(task_text):
            return self._large(
                "Task mentions reasoning beyond extraction or formatting."
            )
        return RoutingDecision(
            self.small_model,
            "small",
            f"Explicit {context.task_kind} task with bounded criteria and read-only tools.",
        )

    def _large(self, reason: str) -> RoutingDecision:
        return RoutingDecision(self.large_model, "large", reason)
