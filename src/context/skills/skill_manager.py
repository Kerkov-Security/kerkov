from pathlib import Path
from typing import Dict, List, Optional, Set

from src.context.skills.skill_loader import SkillLoader
from src.models.skill_model import Skill

_BUILTINS_DIR = Path(__file__).parent / "builtins"
_USER_SKILLS_DIR = Path.home() / ".kerkov" / "skills"


class SkillManager:
    """Manages loading, listing, and activating kerkov skills."""

    def __init__(self):
        self._skills: Dict[str, Skill] = {}
        self._active_skill_names: List[str] = []
        self._load_all()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _load_all(self) -> None:
        """Load built-in skills then overlay user-defined skills."""
        self._load_from_dir(_BUILTINS_DIR, is_builtin=True)
        self._load_from_dir(_USER_SKILLS_DIR, is_builtin=False)

    def _load_from_dir(self, directory: Path, is_builtin: bool) -> None:
        if not directory.exists():
            return
        for file in sorted(directory.rglob("*.md")):
            skill = SkillLoader(str(file)).load()
            if skill:
                skill.is_builtin = is_builtin
                self._skills[skill.name] = skill

    def reload(self) -> None:
        """Reload all skills from disk."""
        self._skills.clear()
        self._load_all()

    # ------------------------------------------------------------------
    # Activation
    # ------------------------------------------------------------------

    @property
    def active_skill(self) -> Optional[Skill]:
        if len(self._active_skill_names) == 0:
            return None
        return self._skills.get(self._active_skill_names[-1])

    @property
    def active_skills(self) -> List[Skill]:
        return [self._skills[name] for name in self._active_skill_names if name in self._skills]

    def activate(self, name: str) -> Optional[Skill]:
        """Activate exactly one skill by name. Returns the skill or None if not found."""
        if name not in self._skills:
            return None
        self._active_skill_names = [name]
        return self._skills[name]

    def add_active_skill(self, name: str) -> Optional[Skill]:
        """Add a skill to the active set. Returns the skill or None if not found."""
        if name not in self._skills:
            return None
        if name not in self._active_skill_names:
            self._active_skill_names.append(name)
        return self._skills[name]

    def remove_active_skill(self, name: str) -> bool:
        """Remove a skill from the active set. Returns True if removed."""
        if name not in self._active_skill_names:
            return False
        self._active_skill_names = [n for n in self._active_skill_names if n != name]
        return True

    def clear_active_skills(self) -> None:
        """Clear all active skills."""
        self._active_skill_names = []

    def deactivate(self) -> None:
        """Deactivate all active skills."""
        self.clear_active_skills()

    def get_allowed_tool_names(self) -> Optional[Set[str]]:
        """
        Return merged allowed tools for active skills.

        Returns:
            - None: no restrictions (no active skills OR at least one skill allows all tools)
            - set[str]: union of explicitly allowed tools across active skills
        """
        skills = self.active_skills
        if len(skills) == 0:
            return None

        allowed: Set[str] = set()
        for skill in skills:
            if not skill.tools:
                return None
            allowed.update(skill.tools)

        return allowed

    def _format_single_skill_prompt(self, skill: Skill) -> str:
        return f"\n\n[Active Skill: {skill.name}]\n{skill.content}"

    def _format_multi_skill_prompt(self, skills: List[Skill]) -> str:
        lines = [f"\n\n[Active Skills: {', '.join([s.name for s in skills])}]"]
        for skill in skills:
            lines.append(f"\n[Skill: {skill.name}]\n{skill.content}")
        return "".join(lines)

    def to_prompt_string(self) -> Optional[str]:
        """Return skill prompt block to append to the system message, or None."""
        skills = self.active_skills
        if len(skills) == 0:
            return None

        if len(skills) == 1:
            return self._format_single_skill_prompt(skills[0])

        return self._format_multi_skill_prompt(skills)

    # ------------------------------------------------------------------
    # Querying
    # ------------------------------------------------------------------

    def get_skill(self, name: str) -> Optional[Skill]:
        return self._skills.get(name)

    def list_skills(self) -> Dict[str, Skill]:
        return self._skills.copy()

    def list_by_tag(self, tag: str) -> List[Skill]:
        return [s for s in self._skills.values() if tag in s.tags]

