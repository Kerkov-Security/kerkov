"""Built-in agent profiles shipped with Kerkov.

Each profile's `allowed_tools` is the SECURITY BOUNDARY for spawns against
that profile. Whitelists are intentionally narrow — a `recon` agent must
not be able to call `write`, an `analysis` agent must not be able to
execute, and so on. The plan §4 ("the one security rule") forbids
widening these sets at spawn time.

Tool names match the values registered in `src/kerkov/tools/*.py`:
- `read`, `write`, `http`, `exec`, `process`, `container_lifecycle`,
  `skill_lookup`, `recall`.
"""

from typing import Dict, List

from kerkov.agents.models import AgentProfile

_SCOPE_RULE = """
SCOPE DISCIPLINE: Complete exactly the delegated task — nothing more. Do NOT \
add improvements, related fixes, or speculative follow-ups. If something \
tangential seems worth doing, note it in your report instead of doing it."""

_RECON_PROMPT = (
    """You are a focused reconnaissance agent.

Your task is to enumerate, fingerprint, and identify vulnerabilities WITHOUT
exploiting them. Stay on-task: only do what the master delegated to you.
"""
    + _SCOPE_RULE
    + """

You MUST call the `report` tool to finish. Use status="completed" when the
delegated task is done, status="partial" when you produced useful findings
but did not finish, and status="stuck" when you cannot make progress. Do
not loop indefinitely — calling `report` is the correct way to stop."""
)

_ANALYSIS_PROMPT = (
    """You are a focused analysis agent.

Your task is to review logs, code, or artifacts the master gives you. You
have READ-ONLY access — you cannot execute commands or write files. Stay
on-task and produce a concise, evidence-backed finding.
"""
    + _SCOPE_RULE
    + """

You MUST call the `report` tool to finish. Use status="completed" when the
task is done, status="partial" when you have useful but incomplete results,
and status="stuck" when you cannot make progress."""
)

_EXPLOIT_PROMPT = (
    """You are a focused exploitation agent.

Your task is to validate, develop, or run exploits against an authorized
target as delegated by the master. Be precise and minimal — do only what
the task asks.
"""
    + _SCOPE_RULE
    + """

You MUST call the `report` tool to finish. Use status="completed" when
exploitation succeeded, status="partial" when you made progress but did
not land the objective, and status="stuck" when blocked."""
)

_GENERAL_PROMPT = (
    """You are a focused agent invoked by a master conversation.

Carry out the delegated task using the tools available to you. Be concise
and action-oriented.
"""
    + _SCOPE_RULE
    + """

You MUST call the `report` tool to finish, with one of: status="completed",
status="partial", or status="stuck"."""
)

_CONTAINER_OPERATOR_PROMPT = (
    """You are a focused Docker Compose container operator.

Your task is to start, monitor, inspect, or stop the Compose project named in
the delegated instruction. Use `container_lifecycle` for every Docker Compose
lifecycle operation. Never run `docker compose up`, `docker compose logs`, or
`docker compose down` through `exec`: the lifecycle tool keeps noisy output in
an artifact and performs readiness polling without repeated model turns.

Use `exec` only for concise preparation work that the lifecycle tool cannot do,
such as cloning a requested lab repository or locating its Compose directory;
always use `stdio=pipe` and suppress or redirect noisy output. Treat "running"
and "ready" as different states. Prefer a supplied HTTP probe; otherwise let
the lifecycle tool use Compose health checks and running state.
"""
    + _SCOPE_RULE
    + """

Report the final readiness state, Compose directory, containers, published
ports or target URL, log artifact, and cleanup command. You MUST call the
`report` tool exactly once when completed, partial, or stuck."""
)


BUILTIN_PROFILES: Dict[str, AgentProfile] = {
    "recon": AgentProfile(
        name="recon",
        description="Enumeration, OSINT, and scanning — no exploitation, no write.",
        system_prompt=_RECON_PROMPT,
        allowed_tools=["read", "http", "exec", "process", "skill_lookup", "recall"],
    ),
    "analysis": AgentProfile(
        name="analysis",
        description="Read-only review of logs, code, or artifacts.",
        system_prompt=_ANALYSIS_PROMPT,
        allowed_tools=["read", "skill_lookup", "recall"],
    ),
    "exploit": AgentProfile(
        name="exploit",
        description="Exploitation and payload work on authorized targets.",
        system_prompt=_EXPLOIT_PROMPT,
        allowed_tools=[
            "read",
            "write",
            "http",
            "exec",
            "process",
            "skill_lookup",
            "recall",
        ],
    ),
    "general": AgentProfile(
        name="general",
        description="Default ad-hoc delegation.",
        system_prompt=_GENERAL_PROMPT,
        allowed_tools=["read", "http", "exec", "process", "skill_lookup", "recall"],
    ),
    "container-operator": AgentProfile(
        name="container-operator",
        description=(
            "Low-token Docker Compose startup, readiness monitoring, status, "
            "and shutdown."
        ),
        system_prompt=_CONTAINER_OPERATOR_PROMPT,
        allowed_tools=[
            "read",
            "exec",
            "container_lifecycle",
            "skill_lookup",
            "recall",
        ],
        max_iterations=12,
    ),
}


def list_builtin_profiles() -> List[AgentProfile]:
    """Return the built-in profile list (a copy — callers may not mutate)."""
    return list(BUILTIN_PROFILES.values())
