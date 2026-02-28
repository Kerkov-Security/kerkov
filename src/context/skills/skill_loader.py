from pathlib import Path
from typing import Optional

import yaml

from src.models.skill_model import Skill


class SkillLoader:
    """Parses a skill markdown file with YAML frontmatter into a Skill model."""

    def __init__(self, skill_file: str):
        self.skill_file = Path(skill_file)

    def load(self) -> Optional[Skill]:
        """Load and parse the skill file. Returns None if the file is invalid."""
        try:
            text = self.skill_file.read_text(encoding="utf-8")
        except OSError:
            return None

        frontmatter, content = self._parse_frontmatter(text)

        name = frontmatter.get("name", "").strip()
        if not name:
            name = self.skill_file.stem

        description = frontmatter.get("description", "").strip()
        tags = frontmatter.get("tags") or []
        tools = frontmatter.get("tools") or None

        # Normalize tools to a list of strings if provided
        if tools is not None and not isinstance(tools, list):
            tools = [str(tools)]

        return Skill(
            name=name,
            description=description,
            tags=tags if isinstance(tags, list) else [tags],
            tools=tools,
            content=content.strip(),
            source_path=str(self.skill_file),
        )

    def _parse_frontmatter(self, text: str) -> tuple[dict, str]:
        """Split YAML frontmatter from markdown body."""
        if not text.startswith("---"):
            return {}, text

        closing = text.find("\n---", 3)
        if closing == -1:
            return {}, text

        yaml_str = text[3:closing].strip()
        content = text[closing + 4:].strip()

        try:
            data = yaml.safe_load(yaml_str) or {}
        except yaml.YAMLError:
            data = {}

        return data, content
