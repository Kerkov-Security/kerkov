"""
SkillManager — discovery, parsing, indexing, and search for SKILL.md files.
"""

import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

import yaml

from kerkov.context.skills.models import (
    EligibilityResult,
    IneligibilityReason,
    KerkovMetadata,
    RequiresSpec,
    SkillEntry,
)
from kerkov.context.skills.skill_retriever import SkillRetriever
from kerkov.core.logging.logger_factory import get_logger
from kerkov.core.process.shell_wrap import default_shell
from kerkov.core.project_paths import resolve_project_dir
from kerkov.utils.privileges import is_process_elevated

logger = get_logger("SkillManager")

MAX_SKILL_FILE_BYTES = 256_000
MAX_SKILLS_LOADED = 200
MAX_SKILLS_IN_PROMPT = 100
MAX_SKILLS_PROMPT_CHARS = 20_000
MAX_CATALOG_DESC_CHARS = 80


class SkillManager:
    """Discovers, parses, and indexes SKILL.md files from a skills directory."""

    def __init__(self, skills_dir: Optional[str] = None):
        if skills_dir is None:
            skills_dir = str(resolve_project_dir("skills", Path(__file__)))
        self._skills_dir = Path(skills_dir)
        self._skills: dict[str, SkillEntry] = {}
        self._scan()
        self._retriever = SkillRetriever(list(self._skills.values()))

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
                logger.warning(f"Skipping {path}: file too large ({file_size} bytes)")
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
                        tactic=raw_kerkov.get("tactic"),
                        mitre=raw_kerkov.get("mitre"),
                        difficulty=raw_kerkov.get("difficulty"),
                        platforms=raw_kerkov.get("platforms"),
                        tags=raw_kerkov.get("tags"),
                        products=raw_kerkov.get("products"),
                        vuln_classes=raw_kerkov.get("vuln_classes"),
                        cves=raw_kerkov.get("cves"),
                        lab_sources=raw_kerkov.get("lab_sources"),
                        safe_verification=raw_kerkov.get("safe_verification"),
                        requires=requires,
                    )

            resolved = path.resolve()
            return SkillEntry(
                name=name,
                description=description,
                user_invocable=meta.get("user-invocable", True),
                disable_model_invocation=meta.get("disable-model-invocation", False),
                kerkov=kerkov_meta,
                file_path=str(resolved),
                skill_dir=str(resolved.parent),
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
        match = re.match(
            r"\s*^---[ \t]*\r?\n(.+?)^---[ \t]*\r?\n(.*)", raw, re.DOTALL | re.MULTILINE
        )
        if not match:
            return None, raw

        frontmatter = match.group(1).strip()
        content = match.group(2).strip()
        return frontmatter, content

    def list_skills(self) -> list[SkillEntry]:
        """Return all loaded skills."""
        return list(self._skills.values())

    def load_skill(self, skill_id: str) -> Optional[str]:
        """Render runtime placeholders without changing the stored skill."""
        entry = self._skills.get(skill_id)
        if entry is None:
            return None
        if sys.platform != "win32":
            python = shlex.quote(sys.executable)
        elif default_shell() in ("powershell", "pwsh"):
            python = "& '" + sys.executable.replace("'", "''") + "'"
        else:
            python = subprocess.list2cmdline([sys.executable])
        return entry.content.replace("<kerkov_python>", python)

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

        Delegates to SkillRetriever: synonym-expanded keyword ranking (exact
        name match first) combined with hard tag/category/difficulty filters.
        Returns empty when no query and no filters are given.
        """
        ranked = self._retriever.rank(
            query, tags=tags, category=category, difficulty=difficulty
        )
        return [entry for _, entry in ranked]

    def check_eligibility(self, entry: SkillEntry) -> EligibilityResult:
        """Check whether a skill's runtime requirements (bins, elevation) are satisfied."""
        requires = entry.kerkov.requires if entry.kerkov else None

        missing = (
            [b for b in (requires.bins or []) if shutil.which(b) is None]
            if requires
            else []
        )

        reasons: list[IneligibilityReason] = []
        if missing:
            reasons.append(IneligibilityReason.MISSING_BINS)
        if requires and requires.elevated and not is_process_elevated():
            reasons.append(IneligibilityReason.NEEDS_ELEVATION)

        return EligibilityResult(
            eligible=not reasons,
            reasons=reasons,
            missing_bins=missing,
        )

    def get_catalog_summary(self) -> str:
        """Build a thin skill catalog (name + one-liner) for prompt injection.

        A table of contents only: full descriptions and instructions are
        pulled on demand through skill_lookup, so prompt size stays flat as
        the skill library grows.
        """
        if not self._skills:
            return ""

        lines = [
            "SKILLS CATALOG:",
            'You have access to security skill files via the "skill_lookup" tool.',
            'This is a thin catalog; use skill_lookup action "search" to find the '
            'right skill and action "load" to pull its full instructions before '
            "applying a technique.",
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

            one_liner = entry.description.splitlines()[0] if entry.description else ""
            if len(one_liner) > MAX_CATALOG_DESC_CHARS:
                one_liner = one_liner[: MAX_CATALOG_DESC_CHARS - 1].rstrip() + "…"

            line = f"- {entry.name}: {one_liner}{self._eligibility_tag(entry)}"

            if total_chars + len(line) > MAX_SKILLS_PROMPT_CHARS:
                lines.append(f"  ... and {len(self._skills) - count} more skills")
                break

            lines.append(line)
            total_chars += len(line)
            count += 1

        return "\n".join(lines)

    def _eligibility_tag(self, entry: SkillEntry) -> str:
        """Render difficulty and any ineligibility hints as an inline suffix."""
        difficulty_tag = ""
        if entry.kerkov and entry.kerkov.difficulty:
            difficulty_tag = f" [{entry.kerkov.difficulty}]"

        eligibility = self.check_eligibility(entry)
        ineligible_tag = ""
        if not eligibility.eligible:
            parts = []
            if IneligibilityReason.MISSING_BINS in eligibility.reasons:
                parts.append(f"missing: {', '.join(eligibility.missing_bins)}")
            if IneligibilityReason.NEEDS_ELEVATION in eligibility.reasons:
                parts.append("needs elevated privileges")
            ineligible_tag = f" ({'; '.join(parts)})"

        return f"{difficulty_tag}{ineligible_tag}"
