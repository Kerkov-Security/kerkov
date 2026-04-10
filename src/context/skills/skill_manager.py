"""
SkillManager — discovery, parsing, indexing, and search for SKILL.md files.
"""

import re
import shutil
from pathlib import Path
from typing import Optional

import yaml

from src.core.logging.logger_factory import get_logger
from src.context.skills.models import (
    EligibilityResult,
    KerkovMetadata,
    RequiresSpec,
    SkillEntry,
)

logger = get_logger("SkillManager")

MAX_SKILL_FILE_BYTES = 256_000
MAX_SKILLS_LOADED = 200
MAX_SKILLS_IN_PROMPT = 100
MAX_SKILLS_PROMPT_CHARS = 20_000

# Default skills directory: top-level skills/ in the project root
_DEFAULT_SKILLS_DIR = str(Path(__file__).resolve().parents[3] / "skills")


class SkillManager:
    """Discovers, parses, and indexes SKILL.md files from a skills directory."""

    def __init__(self, skills_dir: Optional[str] = None):
        if skills_dir is None:
            skills_dir = _DEFAULT_SKILLS_DIR
        self._skills_dir = Path(skills_dir)
        self._skills: dict[str, SkillEntry] = {}
        self._scan()

    def _scan(self) -> None:
        """Scan the skills directory and parse all SKILL.md files."""
        if not self._skills_dir.is_dir():
            logger.warning(f"Skills directory not found: {self._skills_dir}")
            return

        loaded = 0
        for skill_dir in sorted(self._skills_dir.iterdir()):
            if loaded >= MAX_SKILLS_LOADED:
                logger.warning(
                    f"Reached max skills limit ({MAX_SKILLS_LOADED}), skipping remaining"
                )
                break

            skill_file = skill_dir / "SKILL.md"
            if not skill_file.is_file():
                continue

            entry = self._parse_skill_file(skill_file)
            if entry:
                if entry.name in self._skills:
                    logger.warning(
                        f"Duplicate skill name '{entry.name}' from {skill_file}, "
                        f"overwriting previous from {self._skills[entry.name].file_path}"
                    )
                self._skills[entry.name] = entry
                loaded += 1

        logger.info(f"Loaded {loaded} skill(s) from {self._skills_dir}")

    def _parse_skill_file(self, path: Path) -> Optional[SkillEntry]:
        """Parse a SKILL.md file into a SkillEntry."""
        try:
            file_size = path.stat().st_size
            if file_size > MAX_SKILL_FILE_BYTES:
                logger.warning(
                    f"Skipping {path}: file too large ({file_size} bytes)"
                )
                return None

            raw = path.read_text(encoding="utf-8")
            frontmatter, content = self._split_frontmatter(raw)
            if frontmatter is None:
                logger.warning(f"Skipping {path}: no valid YAML frontmatter")
                return None

            meta = yaml.safe_load(frontmatter)
            if not isinstance(meta, dict):
                logger.warning(f"Skipping {path}: frontmatter is not a mapping")
                return None

            name = meta.get("name")
            description = meta.get("description")
            if not name or not description:
                logger.warning(
                    f"Skipping {path}: missing required field (name or description)"
                )
                return None

            kerkov_meta = None
            raw_metadata = meta.get("metadata", {})
            if isinstance(raw_metadata, dict):
                raw_kerkov = raw_metadata.get("kerkov")
                if isinstance(raw_kerkov, dict):
                    requires = None
                    raw_requires = raw_kerkov.get("requires")
                    if isinstance(raw_requires, dict):
                        requires = RequiresSpec(**raw_requires)
                    kerkov_meta = KerkovMetadata(
                        category=raw_kerkov.get("category"),
                        mitre=raw_kerkov.get("mitre"),
                        difficulty=raw_kerkov.get("difficulty"),
                        platforms=raw_kerkov.get("platforms"),
                        tags=raw_kerkov.get("tags"),
                        requires=requires,
                    )

            return SkillEntry(
                name=name,
                description=description,
                user_invocable=meta.get("user-invocable", True),
                disable_model_invocation=meta.get("disable-model-invocation", False),
                kerkov=kerkov_meta,
                file_path=str(path.resolve()),
                content=content,
            )

        except yaml.YAMLError as e:
            logger.warning(f"Skipping {path}: YAML parse error: {e}")
            return None
        except Exception as e:
            logger.warning(f"Skipping {path}: unexpected error: {e}")
            return None

    @staticmethod
    def _split_frontmatter(raw: str) -> tuple[Optional[str], str]:
        """Split a markdown file into YAML frontmatter and body content."""
        match = re.match(r"\s*^---[ \t]*\r?\n(.+?)^---[ \t]*\r?\n(.*)", raw, re.DOTALL | re.MULTILINE)
        if not match:
            return None, raw

        frontmatter = match.group(1).strip()
        content = match.group(2).strip()
        return frontmatter, content

    def list_skills(self) -> list[SkillEntry]:
        """Return all loaded skills."""
        return list(self._skills.values())

    def load_skill(self, skill_id: str) -> Optional[str]:
        """Return the full markdown content of a skill, or None if not found."""
        entry = self._skills.get(skill_id)
        return entry.content if entry else None

    def get_skill(self, skill_id: str) -> Optional[SkillEntry]:
        """Return a SkillEntry by name, or None if not found."""
        return self._skills.get(skill_id)

    def search(
        self,
        query: Optional[str] = None,
        tags: Optional[list[str]] = None,
        category: Optional[str] = None,
        difficulty: Optional[str] = None,
    ) -> list[SkillEntry]:
        """Search skills by keyword, tags, category, or difficulty.

        Results are ranked: exact name match first, then description hits,
        then tag/category hits.
        """
        results: list[tuple[int, SkillEntry]] = []

        for entry in self._skills.values():
            score = self._match_score(entry, query, tags, category, difficulty)
            if score > 0:
                results.append((score, entry))

        results.sort(key=lambda x: x[0], reverse=True)
        return [entry for _, entry in results]

    @staticmethod
    def _match_score(
        entry: SkillEntry,
        query: Optional[str],
        tags: Optional[list[str]],
        category: Optional[str],
        difficulty: Optional[str],
    ) -> int:
        """Compute a match score for a skill against the given filters."""
        score = 0
        query_lower = query.lower() if query else None

        if query_lower:
            if entry.name.lower() == query_lower:
                score += 10
            elif query_lower in entry.name.lower():
                score += 5
            if query_lower in entry.description.lower():
                score += 3

            if entry.kerkov:
                if entry.kerkov.tags:
                    for tag in entry.kerkov.tags:
                        if query_lower in tag.lower():
                            score += 2
                if entry.kerkov.category and query_lower in entry.kerkov.category.lower():
                    score += 2

        if tags and entry.kerkov and entry.kerkov.tags:
            entry_tags_lower = {t.lower() for t in entry.kerkov.tags}
            for t in tags:
                if t.lower() in entry_tags_lower:
                    score += 2

        if category and entry.kerkov and entry.kerkov.category:
            if category.lower() in entry.kerkov.category.lower():
                score += 2

        if difficulty and entry.kerkov and entry.kerkov.difficulty:
            if difficulty.lower() == entry.kerkov.difficulty.lower():
                score += 2

        return score

    def check_eligibility(self, entry: SkillEntry) -> EligibilityResult:
        """Check whether a skill's required binaries are available on PATH."""
        if not entry.kerkov or not entry.kerkov.requires or not entry.kerkov.requires.bins:
            return EligibilityResult(eligible=True)

        missing = [b for b in entry.kerkov.requires.bins if shutil.which(b) is None]
        return EligibilityResult(eligible=len(missing) == 0, missing_bins=missing)

    def get_index_summary(self) -> str:
        """Build a compact skill index for system prompt injection."""
        if not self._skills:
            return ""

        lines = [
            "SKILLS DATABASE:",
            'You have access to security skill files via the "skill_lookup" tool.',
            "When approaching a technique, load the relevant skill first.",
            "",
            "Available skills:",
        ]

        count = 0
        total_chars = 0
        for entry in self._skills.values():
            if entry.disable_model_invocation:
                continue
            if count >= MAX_SKILLS_IN_PROMPT:
                break

            eligibility = self.check_eligibility(entry)
            difficulty_tag = ""
            if entry.kerkov and entry.kerkov.difficulty:
                difficulty_tag = f" [{entry.kerkov.difficulty}]"

            missing_tag = ""
            if not eligibility.eligible:
                missing_tag = f" (missing: {', '.join(eligibility.missing_bins)})"

            line = f"- {entry.name}: {entry.description}{difficulty_tag}{missing_tag}"

            if total_chars + len(line) > MAX_SKILLS_PROMPT_CHARS:
                lines.append(f"  ... and {len(self._skills) - count} more skills")
                break

            lines.append(line)
            total_chars += len(line)
            count += 1

        lines.append("")
        lines.append('Use skill_lookup with action "load" and the skill name to load full instructions.')
        lines.append('Use skill_lookup with action "search" to find skills by keyword.')

        return "\n".join(lines)
