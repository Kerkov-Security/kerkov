from pathlib import Path
from typing import Dict, List, Optional

from src.context.skills.skill_loader import SkillLoader
from src.models.skill_model import Skill

_BUILTINS_DIR = Path(__file__).parent / "builtins"
_USER_SKILLS_DIR = Path.home() / ".kerkov" / "skills"


class SkillManager:
    """Manages loading, listing, and activating kerkov skills."""

    def __init__(self):
        self._skills: Dict[str, Skill] = {}
        self._active_skill_name: Optional[str] = None
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
        if self._active_skill_name is None:
            return None
        return self._skills.get(self._active_skill_name)

    def activate(self, name: str) -> Optional[Skill]:
        """Activate a skill by name. Returns the skill or None if not found."""
        if name not in self._skills:
            return None
        self._active_skill_name = name
        return self._skills[name]

    def deactivate(self) -> None:
        """Deactivate the current skill."""
        self._active_skill_name = None

    # ------------------------------------------------------------------
    # Querying
    # ------------------------------------------------------------------

    def get_skill(self, name: str) -> Optional[Skill]:
        return self._skills.get(name)

    def list_skills(self) -> Dict[str, Skill]:
        return self._skills.copy()

    def list_by_tag(self, tag: str) -> List[Skill]:
        return [s for s in self._skills.values() if tag in s.tags]

    # ------------------------------------------------------------------
    # Context injection
    # ------------------------------------------------------------------

    def to_prompt_string(self) -> Optional[str]:
        """Return skill prompt block to append to the system message, or None."""
        skill = self.active_skill
        if not skill:
            return None
        return f"\n\n[Active Skill: {skill.name}]\n{skill.content}"
