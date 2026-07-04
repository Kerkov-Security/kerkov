"""AgentManager — discovery and indexing for built-in and user-defined profiles.

Mirrors `SkillManager`: scans an `AGENT.md` directory tree at construction time,
parses YAML frontmatter, exposes `get_profile(name)` and an index summary
suitable for injection into the master's system prompt.

User-defined `AGENT.md` files have the same frontmatter shape as the
`AgentProfile` model:

```
---
name: my-agent
description: One-line summary the master LLM sees in the index.
allowed_tools:
  - read
  - http
max_iterations: 30
---

(System prompt body — anything after the frontmatter is the agent's
system prompt.)
```

If a user-defined profile shares a name with a built-in, the user-defined
one wins (with a warning) — the user explicitly opted in by writing it.
"""

import re
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from pydantic import ValidationError

from kerkov.agents.base_agent import BUILTIN_PROFILES
from kerkov.agents.models import AgentProfile
from kerkov.core.logging.logger_factory import get_logger

logger = get_logger("AgentManager")

MAX_AGENT_FILE_BYTES = 256_000
MAX_AGENTS_LOADED = 200
MAX_AGENTS_IN_PROMPT = 100
MAX_AGENTS_PROMPT_CHARS = 8_000

_DEFAULT_AGENTS_DIR = str(Path(__file__).resolve().parents[2] / "agents")


class AgentManager:
    """Discovers, parses, and indexes built-in profiles + `AGENT.md` files."""

    def __init__(self, agents_dir: Optional[str] = None):
        if agents_dir is None:
            agents_dir = _DEFAULT_AGENTS_DIR
        self._agents_dir = Path(agents_dir)
        self._profiles: Dict[str, AgentProfile] = {}
        self._load_builtins()
        self._scan_user_defined()

    def _load_builtins(self) -> None:
        """Seed the registry with the shipped built-in profiles."""
        for profile in BUILTIN_PROFILES.values():
            self._profiles[profile.name] = profile

    def _scan_user_defined(self) -> None:
        """Walk the agents directory and parse all `AGENT.md` files."""
        if not self._agents_dir.is_dir():
            logger.info(f"Agents directory not found (skipping): {self._agents_dir}")
            return

        loaded = 0
        for entry in sorted(self._agents_dir.iterdir()):
            if loaded >= MAX_AGENTS_LOADED:
                logger.warning(
                    f"Reached max agents limit ({MAX_AGENTS_LOADED}), skipping rest"
                )
                break

            agent_file = entry / "AGENT.md" if entry.is_dir() else entry
            if agent_file.name != "AGENT.md" or not agent_file.is_file():
                continue

            profile = self._parse_agent_file(agent_file)
            if profile:
                if (
                    profile.name in self._profiles
                    and self._profiles[profile.name].source == "builtin"
                ):
                    logger.warning(
                        f"User-defined agent '{profile.name}' from {agent_file} "
                        "shadows a built-in profile"
                    )
                elif profile.name in self._profiles:
                    logger.warning(
                        f"Duplicate agent name '{profile.name}' from {agent_file}, "
                        f"overwriting previous from {self._profiles[profile.name].source}"
                    )
                self._profiles[profile.name] = profile
                loaded += 1

        logger.info(
            f"Loaded {loaded} user-defined agent(s) from {self._agents_dir} "
            f"({len(BUILTIN_PROFILES)} built-in)"
        )

    def _parse_agent_file(self, path: Path) -> Optional[AgentProfile]:
        """Parse an AGENT.md file into an `AgentProfile`."""
        try:
            size = path.stat().st_size
            if size > MAX_AGENT_FILE_BYTES:
                logger.warning(f"Skipping {path}: file too large ({size} bytes)")
                return None

            raw = path.read_text(encoding="utf-8")
            frontmatter, body = self._split_frontmatter(raw)
            if frontmatter is None:
                logger.warning(f"Skipping {path}: no valid YAML frontmatter")
                return None

            meta = yaml.safe_load(frontmatter)
            if not isinstance(meta, dict):
                logger.warning(f"Skipping {path}: frontmatter is not a mapping")
                return None

            name = meta.get("name")
            description = meta.get("description")
            allowed_tools = meta.get("allowed_tools")
            if not name or not description or not isinstance(allowed_tools, list):
                logger.warning(
                    f"Skipping {path}: missing required field "
                    "(name, description, or allowed_tools list)"
                )
                return None

            system_prompt = (meta.get("system_prompt") or body).strip()
            if not system_prompt:
                logger.warning(
                    f"Skipping {path}: empty system prompt (body and field both empty)"
                )
                return None

            try:
                return AgentProfile(
                    name=str(name),
                    description=str(description),
                    system_prompt=system_prompt,
                    allowed_tools=[str(t) for t in allowed_tools],
                    max_iterations=int(meta.get("max_iterations", 30)),
                    source=str(path.resolve()),
                )
            except ValidationError as e:
                logger.warning(f"Skipping {path}: invalid AgentProfile fields: {e}")
                return None

        except yaml.YAMLError as e:
            logger.warning(f"Skipping {path}: YAML parse error: {e}")
            return None
        except Exception as e:
            logger.warning(f"Skipping {path}: unexpected error: {e}")
            return None

    @staticmethod
    def _split_frontmatter(raw: str) -> tuple[Optional[str], str]:
        """Split a markdown file into YAML frontmatter and body."""
        match = re.match(
            r"\s*^---[ \t]*\r?\n(.+?)^---[ \t]*\r?\n(.*)",
            raw,
            re.DOTALL | re.MULTILINE,
        )
        if not match:
            return None, raw
        return match.group(1).strip(), match.group(2).strip()

    def list_profiles(self) -> List[AgentProfile]:
        """Return all loaded profiles (built-in + user-defined)."""
        return list(self._profiles.values())

    def get_profile(self, name: str) -> Optional[AgentProfile]:
        """Return a profile by name, or None if not found."""
        return self._profiles.get(name)

    def get_index_summary(self) -> str:
        """Build a compact agent-profile index for system prompt injection.

        Master LLMs read this to know which profiles exist and what their
        capabilities are. Truncated past `MAX_AGENTS_PROMPT_CHARS` so a
        large user directory cannot blow up the master's context.
        """
        if not self._profiles:
            return ""

        lines = [
            "AGENT PROFILES:",
            "You may delegate a self-contained sub-task to an agent via the "
            + '"delegate" tool. The agent runs in isolation with its own '
            + "tool set and reports back a structured result.",
            "",
            "Available profiles:",
        ]

        count = 0
        total = 0
        for profile in self._profiles.values():
            if count >= MAX_AGENTS_IN_PROMPT:
                break
            tools = ", ".join(profile.allowed_tools) or "(none)"
            line = f"- {profile.name}: {profile.description} — tools: [{tools}]"
            if total + len(line) > MAX_AGENTS_PROMPT_CHARS:
                lines.append(f"  ... and {len(self._profiles) - count} more")
                break
            lines.append(line)
            total += len(line)
            count += 1

        lines.append("")
        lines.append(
            "Pick the narrowest profile that fits the task. A single "
            + '"delegate" call may carry multiple tasks for parallel investigation.'
        )
        return "\n".join(lines)
