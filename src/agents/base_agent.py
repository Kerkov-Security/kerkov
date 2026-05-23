"""Built-in agent profiles shipped with Kerkov.

Each profile's `allowed_tools` is the SECURITY BOUNDARY for spawns against
that profile. Whitelists are intentionally narrow — a `recon` agent must
not be able to call `write`, an `analysis` agent must not be able to
execute, and so on. The plan §4 ("the one security rule") forbids
widening these sets at spawn time.

Tool names match the values registered in `src/tools/*.py`:
- `read`, `write`, `http`, `crypto_lab`, `exec`, `process`, `skill_lookup`.
"""

from typing import Dict, List

from src.agents.models import AgentProfile


_RECON_PROMPT = """You are a focused reconnaissance agent.

Your task is to enumerate, fingerprint, and identify vulnerabilities WITHOUT
exploiting them. Stay on-task: only do what the master delegated to you.

You MUST call the `report` tool to finish. Use status="completed" when the
delegated task is done, status="partial" when you produced useful findings
but did not finish, and status="stuck" when you cannot make progress. Do
not loop indefinitely — calling `report` is the correct way to stop."""

_ANALYSIS_PROMPT = """You are a focused analysis agent.

Your task is to review logs, code, or artifacts the master gives you. You
have READ-ONLY access — you cannot execute commands or write files. Stay
on-task and produce a concise, evidence-backed finding.

You MUST call the `report` tool to finish. Use status="completed" when the
task is done, status="partial" when you have useful but incomplete results,
and status="stuck" when you cannot make progress."""

_EXPLOIT_PROMPT = """You are a focused exploitation agent.

Your task is to validate, develop, or run exploits against an authorized
target as delegated by the master. Be precise and minimal — do only what
the task asks.

You MUST call the `report` tool to finish. Use status="completed" when
exploitation succeeded, status="partial" when you made progress but did
not land the objective, and status="stuck" when blocked."""

_GENERAL_PROMPT = """You are a focused agent invoked by a master conversation.

Carry out the delegated task using the tools available to you. Be concise
and action-oriented.

You MUST call the `report` tool to finish, with one of: status="completed",
status="partial", or status="stuck"."""


BUILTIN_PROFILES: Dict[str, AgentProfile] = {
    "recon": AgentProfile(
        name="recon",
        description="Enumeration, OSINT, and scanning — no exploitation, no write.",
        system_prompt=_RECON_PROMPT,
        allowed_tools=["read", "http", "exec", "process", "skill_lookup"],
    ),
    "analysis": AgentProfile(
        name="analysis",
        description="Read-only review of logs, code, or artifacts.",
        system_prompt=_ANALYSIS_PROMPT,
        allowed_tools=["read", "skill_lookup"],
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
            "crypto_lab",
            "skill_lookup",
        ],
    ),
    "general": AgentProfile(
        name="general",
        description="Default ad-hoc delegation.",
        system_prompt=_GENERAL_PROMPT,
        allowed_tools=["read", "http", "exec", "process", "skill_lookup"],
    ),
}


def list_builtin_profiles() -> List[AgentProfile]:
    """Return the built-in profile list (a copy — callers may not mutate)."""
    return list(BUILTIN_PROFILES.values())
