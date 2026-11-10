"""Tests for SkillManager."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kerkov.context.skills.models import (
    IneligibilityReason,
    KerkovMetadata,
    RequiresSpec,
    SkillEntry,
)
from kerkov.context.skills.skill_manager import SkillManager

VALID_SKILL_MD = """\
---
name: test-skill
description: "A test skill for unit tests"
user-invocable: true
disable-model-invocation: false
metadata:
  kerkov:
    category: recon/local
    tactic: discovery
    mitre: [T1046, T1595]
    difficulty: beginner
    platforms: [linux, windows]
    tags: [recon, test]
    products: [nginx]
    vuln_classes: [network-service-discovery]
    cves: [CVE-2024-9999]
    lab_sources: [vulhub]
    safe_verification: true
    requires:
      bins: [nmap]
      credentials: [HTB_API_TOKEN]
---

# Test Skill

Some instructions here.
"""

MINIMAL_SKILL_MD = """\
---
name: minimal
description: "Minimal skill"
---

Minimal content.
"""

NO_FRONTMATTER_MD = """\
# No Frontmatter

Just content.
"""

MISSING_NAME_MD = """\
---
description: "Missing name field"
---

Content.
"""

MISSING_DESCRIPTION_MD = """\
---
name: no-desc
---

Content.
"""

MODEL_DISABLED_SKILL_MD = """\
---
name: hidden-skill
description: "Not for the model"
disable-model-invocation: true
---

Secret content.
"""

NOT_USER_INVOCABLE_MD = """\
---
name: internal-skill
description: "Internal only"
user-invocable: false
---

Internal content.
"""

FRONTMATTER_WITH_HR_MD = """\
---
name: hr-skill
description: "Skill with horizontal rule in body"
---

# Heading

Some text.

---

More text after horizontal rule.
"""

FRONTMATTER_WITH_DASHES_IN_YAML_MD = """\
---
name: dashes-skill
description: "foo --- bar"
---

Body content here.
"""


EXTERNAL_SKILL_MD = """\
---
name: pdf-review
description: "Externally installed skill with no Kerkov metadata"
license: MIT
allowed-tools: [Bash, Read]
compatibility: ">=1.0"
---

Read the PDF and summarize it.
"""

SHADOWING_SKILL_MD = """\
---
name: minimal
description: "External skill claiming a shipped name"
---

External body.
"""

BAD_NAME_SKILL_MD = """\
---
name: "../escape"
description: "Skill with an unusable name"
---

Body.
"""

NON_STRING_DESCRIPTION_MD = """\
---
name: listy
description:
  - one
  - two
---

Body.
"""


class SkillManagerRootsTest(unittest.TestCase):

    def test_external_root_merges_with_primary_root(self):
        with (
            tempfile.TemporaryDirectory() as primary,
            tempfile.TemporaryDirectory() as external,
        ):
            _create_skill_dir(primary, "minimal", MINIMAL_SKILL_MD)
            _create_skill_dir(external, "pdf-review", EXTERNAL_SKILL_MD)

            mgr = SkillManager(skills_dir=primary, extra_dirs=[external])

            names = {entry.name for entry in mgr.list_skills()}
            assert names == {"minimal", "pdf-review"}
            assert mgr.get_skill("minimal").source == "skills"

            imported = mgr.get_skill("pdf-review")
            assert imported.source == "external"
            assert imported.source_root == str(Path(external).resolve())
            assert imported.kerkov is None
            assert mgr.load_skill("pdf-review").startswith("Read the PDF")

    def test_external_root_cannot_shadow_an_earlier_root(self):
        with (
            tempfile.TemporaryDirectory() as primary,
            tempfile.TemporaryDirectory() as external,
        ):
            _create_skill_dir(primary, "minimal", MINIMAL_SKILL_MD)
            _create_skill_dir(external, "minimal", SHADOWING_SKILL_MD)

            mgr = SkillManager(skills_dir=primary, extra_dirs=[external])

            entry = mgr.get_skill("minimal")
            assert entry.source == "skills"
            assert entry.description == "Minimal skill"

            collisions = mgr.collisions()
            assert len(collisions) == 1
            assert collisions[0].name == "minimal"
            assert collisions[0].selected_source == "skills"
            assert collisions[0].shadowed_source == "external"
            assert collisions[0].shadowed_path.startswith(str(Path(external).resolve()))

    def test_extra_roots_keep_configured_order(self):
        with (
            tempfile.TemporaryDirectory() as primary,
            tempfile.TemporaryDirectory() as first,
            tempfile.TemporaryDirectory() as second,
        ):
            _create_skill_dir(first, "minimal", MINIMAL_SKILL_MD)
            _create_skill_dir(second, "minimal", SHADOWING_SKILL_MD)

            mgr = SkillManager(skills_dir=primary, extra_dirs=[first, second])

            entry = mgr.get_skill("minimal")
            assert entry.description == "Minimal skill"
            assert entry.file_path.startswith(str(Path(first).resolve()))

    def test_duplicate_roots_are_scanned_once(self):
        with tempfile.TemporaryDirectory() as primary:
            _create_skill_dir(primary, "minimal", MINIMAL_SKILL_MD)

            mgr = SkillManager(skills_dir=primary, extra_dirs=[primary, primary])

            assert len(mgr.roots()) == 1
            assert mgr.collisions() == []

    def test_blank_extra_roots_are_ignored(self):
        with tempfile.TemporaryDirectory() as primary:
            mgr = SkillManager(skills_dir=primary, extra_dirs=["", "   "])

            assert len(mgr.roots()) == 1

    def test_missing_external_root_does_not_block_valid_skills(self):
        with tempfile.TemporaryDirectory() as primary:
            _create_skill_dir(primary, "minimal", MINIMAL_SKILL_MD)

            mgr = SkillManager(
                skills_dir=primary,
                extra_dirs=[os.path.join(primary, "does-not-exist")],
            )

            assert {entry.name for entry in mgr.list_skills()} == {"minimal"}

    def test_malformed_external_skill_does_not_block_valid_skills(self):
        with (
            tempfile.TemporaryDirectory() as primary,
            tempfile.TemporaryDirectory() as external,
        ):
            _create_skill_dir(primary, "minimal", MINIMAL_SKILL_MD)
            _create_skill_dir(external, "broken", NO_FRONTMATTER_MD)
            _create_skill_dir(external, "pdf-review", EXTERNAL_SKILL_MD)

            mgr = SkillManager(skills_dir=primary, extra_dirs=[external])

            assert {entry.name for entry in mgr.list_skills()} == {
                "minimal",
                "pdf-review",
            }

    def test_unusable_skill_name_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            _create_skill_dir(tmp, "escape", BAD_NAME_SKILL_MD)
            _create_skill_dir(tmp, "long", _long_name_skill())

            mgr = SkillManager(skills_dir=tmp)

            assert mgr.list_skills() == []

    def test_non_string_description_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            _create_skill_dir(tmp, "listy", NON_STRING_DESCRIPTION_MD)

            mgr = SkillManager(skills_dir=tmp)

            assert mgr.list_skills() == []

    def test_catalog_limit_applies_across_roots(self):
        with (
            tempfile.TemporaryDirectory() as primary,
            tempfile.TemporaryDirectory() as external,
        ):
            for index in range(3):
                _create_skill_dir(primary, f"p{index}", _named_skill(f"p{index}"))
            for index in range(3):
                _create_skill_dir(external, f"e{index}", _named_skill(f"e{index}"))

            with patch("kerkov.context.skills.skill_manager.MAX_SKILLS_LOADED", 4):
                mgr = SkillManager(skills_dir=primary, extra_dirs=[external])

            names = [entry.name for entry in mgr.list_skills()]
            assert names == ["p0", "p1", "p2", "e0"]

    @unittest.skipIf(sys.platform == "win32", "symlink creation needs privileges")
    def test_symlinked_skill_directory_resolves_companion_files(self):
        with (
            tempfile.TemporaryDirectory() as store,
            tempfile.TemporaryDirectory() as external,
        ):
            real = _create_skill_dir(store, "pdf-review", EXTERNAL_SKILL_MD)
            os.makedirs(os.path.join(real, "scripts"))
            os.symlink(real, os.path.join(external, "pdf-review"))

            mgr = SkillManager(skills_dir=external)

            entry = mgr.get_skill("pdf-review")
            assert entry is not None
            assert Path(entry.skill_dir).resolve() == Path(real).resolve()
            assert (Path(entry.skill_dir) / "scripts").is_dir()

    @unittest.skipIf(sys.platform == "win32", "symlink creation needs privileges")
    def test_broken_skill_symlink_is_skipped(self):
        with tempfile.TemporaryDirectory() as external:
            _create_skill_dir(external, "minimal", MINIMAL_SKILL_MD)
            os.symlink(
                os.path.join(external, "gone"), os.path.join(external, "dangling")
            )

            mgr = SkillManager(skills_dir=external)

            assert {entry.name for entry in mgr.list_skills()} == {"minimal"}


def _named_skill(name: str) -> str:
    return f'---\nname: {name}\ndescription: "Skill {name}"\n---\n\nBody.\n'


def _long_name_skill() -> str:
    return f'---\nname: {"a" * 65}\ndescription: "Too long"\n---\n\nBody.\n'


def _create_skill_dir(base_dir: str, skill_name: str, content: str) -> str:
    """Create a skill directory with a SKILL.md file."""
    skill_dir = os.path.join(base_dir, skill_name)
    os.makedirs(skill_dir, exist_ok=True)
    skill_file = os.path.join(skill_dir, "SKILL.md")
    with open(skill_file, "w", encoding="utf-8") as f:
        f.write(content)
    return skill_dir


class SplitFrontmatterTest(unittest.TestCase):

    def test_valid_frontmatter(self):
        fm, content = SkillManager._split_frontmatter(VALID_SKILL_MD)
        assert fm is not None
        assert "name: test-skill" in fm
        assert "# Test Skill" in content

    def test_no_frontmatter(self):
        fm, content = SkillManager._split_frontmatter(NO_FRONTMATTER_MD)
        assert fm is None
        assert "# No Frontmatter" in content

    def test_minimal_frontmatter(self):
        fm, content = SkillManager._split_frontmatter(MINIMAL_SKILL_MD)
        assert fm is not None
        assert "name: minimal" in fm
        assert "Minimal content." in content

    def test_horizontal_rule_in_body_not_treated_as_closing(self):
        fm, content = SkillManager._split_frontmatter(FRONTMATTER_WITH_HR_MD)
        assert fm is not None
        assert "name: hr-skill" in fm
        assert "More text after horizontal rule." in content

    def test_dashes_in_yaml_value(self):
        fm, content = SkillManager._split_frontmatter(
            FRONTMATTER_WITH_DASHES_IN_YAML_MD
        )
        assert fm is not None
        assert 'description: "foo --- bar"' in fm
        assert "Body content here." in content

    def test_empty_string(self):
        fm, content = SkillManager._split_frontmatter("")
        assert fm is None

    def test_only_opening_delimiter(self):
        fm, content = SkillManager._split_frontmatter("---\nname: x\n")
        assert fm is None

    def test_leading_whitespace(self):
        raw = "\n\n---\nname: ws\ndescription: whitespace\n---\n\nBody."
        fm, content = SkillManager._split_frontmatter(raw)
        assert fm is not None
        assert "name: ws" in fm


class SkillManagerScanTest(unittest.TestCase):

    def test_default_dir_loads_top_level_skills(self):
        mgr = SkillManager()

        names = {skill.name for skill in mgr.list_skills()}
        assert "localhost-scan" in names

    def test_loads_valid_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            _create_skill_dir(tmp, "test-skill", VALID_SKILL_MD)
            mgr = SkillManager(skills_dir=tmp)

            skills = mgr.list_skills()
            assert len(skills) == 1
            assert skills[0].name == "test-skill"
            assert skills[0].description == "A test skill for unit tests"
            assert skills[0].user_invocable is True
            assert skills[0].disable_model_invocation is False

    def test_parses_kerkov_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            _create_skill_dir(tmp, "test-skill", VALID_SKILL_MD)
            mgr = SkillManager(skills_dir=tmp)

            entry = mgr.get_skill("test-skill")
            assert entry is not None
            assert entry.kerkov is not None
            assert entry.kerkov.category == "recon/local"
            assert entry.kerkov.tactic == "discovery"
            assert entry.kerkov.mitre == ["T1046", "T1595"]
            assert entry.kerkov.difficulty == "beginner"
            assert entry.kerkov.tags == ["recon", "test"]
            assert entry.kerkov.products == ["nginx"]
            assert entry.kerkov.vuln_classes == ["network-service-discovery"]
            assert entry.kerkov.cves == ["CVE-2024-9999"]
            assert entry.kerkov.lab_sources == ["vulhub"]
            assert entry.kerkov.safe_verification is True
            assert entry.kerkov.requires is not None
            assert entry.kerkov.requires.bins == ["nmap"]
            assert entry.kerkov.requires.credentials == ["HTB_API_TOKEN"]

    def test_parses_string_mitre_as_single_item_list(self):
        single_mitre = VALID_SKILL_MD.replace("[T1046, T1595]", "T1046")
        with tempfile.TemporaryDirectory() as tmp:
            _create_skill_dir(tmp, "test-skill", single_mitre)
            mgr = SkillManager(skills_dir=tmp)

            entry = mgr.get_skill("test-skill")
            assert entry is not None
            assert entry.kerkov is not None
            assert entry.kerkov.mitre == ["T1046"]

    def test_loads_minimal_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            _create_skill_dir(tmp, "minimal", MINIMAL_SKILL_MD)
            mgr = SkillManager(skills_dir=tmp)

            entry = mgr.get_skill("minimal")
            assert entry is not None
            assert entry.kerkov is None

    def test_skips_no_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmp:
            _create_skill_dir(tmp, "bad", NO_FRONTMATTER_MD)
            mgr = SkillManager(skills_dir=tmp)
            assert len(mgr.list_skills()) == 0

    def test_skips_missing_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            _create_skill_dir(tmp, "noname", MISSING_NAME_MD)
            mgr = SkillManager(skills_dir=tmp)
            assert len(mgr.list_skills()) == 0

    def test_skips_missing_description(self):
        with tempfile.TemporaryDirectory() as tmp:
            _create_skill_dir(tmp, "nodesc", MISSING_DESCRIPTION_MD)
            mgr = SkillManager(skills_dir=tmp)
            assert len(mgr.list_skills()) == 0

    def test_nonexistent_directory(self):
        mgr = SkillManager(skills_dir="/nonexistent/path/to/skills")
        assert len(mgr.list_skills()) == 0

    def test_skips_directories_without_skill_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Create a directory without SKILL.md
            os.makedirs(os.path.join(tmp, "empty-dir"))
            _create_skill_dir(tmp, "valid", MINIMAL_SKILL_MD)
            mgr = SkillManager(skills_dir=tmp)
            assert len(mgr.list_skills()) == 1

    def test_file_size_limit_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Create a skill file larger than 256KB
            large_content = MINIMAL_SKILL_MD + "\n" + ("x" * 300_000)
            _create_skill_dir(tmp, "large", large_content)
            mgr = SkillManager(skills_dir=tmp)
            assert len(mgr.list_skills()) == 0

    def test_duplicate_skill_name_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Two directories with skills that have the same name in frontmatter
            _create_skill_dir(tmp, "aaa-first", MINIMAL_SKILL_MD)
            _create_skill_dir(tmp, "zzz-second", MINIMAL_SKILL_MD)
            mgr = SkillManager(skills_dir=tmp)

            # Both use name "minimal", only one should survive
            assert len(mgr.list_skills()) == 1

    def test_parses_disable_model_invocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            _create_skill_dir(tmp, "hidden", MODEL_DISABLED_SKILL_MD)
            mgr = SkillManager(skills_dir=tmp)

            entry = mgr.get_skill("hidden-skill")
            assert entry is not None
            assert entry.disable_model_invocation is True

    def test_parses_user_invocable_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            _create_skill_dir(tmp, "internal", NOT_USER_INVOCABLE_MD)
            mgr = SkillManager(skills_dir=tmp)

            entry = mgr.get_skill("internal-skill")
            assert entry is not None
            assert entry.user_invocable is False

    def test_populates_skill_dir_to_parent_of_skill_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            created_dir = _create_skill_dir(tmp, "test-skill", VALID_SKILL_MD)
            mgr = SkillManager(skills_dir=tmp)

            entry = mgr.get_skill("test-skill")
            assert entry is not None
            assert os.path.samefile(entry.skill_dir, created_dir)
            assert os.path.samefile(
                entry.file_path, os.path.join(created_dir, "SKILL.md")
            )


class SkillManagerLookupTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        _create_skill_dir(self._tmp, "test-skill", VALID_SKILL_MD)
        _create_skill_dir(self._tmp, "minimal", MINIMAL_SKILL_MD)
        self.mgr = SkillManager(skills_dir=self._tmp)

    def test_get_skill_found(self):
        entry = self.mgr.get_skill("test-skill")
        assert entry is not None
        assert entry.name == "test-skill"

    def test_get_skill_not_found(self):
        assert self.mgr.get_skill("nonexistent") is None

    def test_load_skill_returns_content(self):
        content = self.mgr.load_skill("test-skill")
        assert content is not None
        assert "# Test Skill" in content

    def test_load_skill_not_found(self):
        assert self.mgr.load_skill("nonexistent") is None


class SkillManagerSearchTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        _create_skill_dir(self._tmp, "test-skill", VALID_SKILL_MD)
        _create_skill_dir(self._tmp, "minimal", MINIMAL_SKILL_MD)
        _create_skill_dir(self._tmp, "hidden", MODEL_DISABLED_SKILL_MD)
        self.mgr = SkillManager(skills_dir=self._tmp)

    def test_search_by_exact_name(self):
        results = self.mgr.search(query="test-skill")
        assert len(results) >= 1
        assert results[0].name == "test-skill"

    def test_search_no_filters_returns_empty(self):
        results = self.mgr.search()
        assert len(results) == 0


class SkillManagerEligibilityTest(unittest.TestCase):

    def test_no_requirements_is_eligible(self):
        entry = SkillEntry(
            name="simple",
            description="No bins",
            file_path="/tmp/fake/SKILL.md",
            skill_dir="/tmp/fake",
            content="content",
        )
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SkillManager(skills_dir=tmp)
            result = mgr.check_eligibility(entry)
            assert result.eligible is True
            assert result.missing_bins == []

    def test_empty_bins_is_eligible(self):
        entry = SkillEntry(
            name="empty-bins",
            description="Empty bins list",
            kerkov=KerkovMetadata(requires=RequiresSpec(bins=[])),
            file_path="/tmp/fake/SKILL.md",
            skill_dir="/tmp/fake",
            content="content",
        )
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SkillManager(skills_dir=tmp)
            result = mgr.check_eligibility(entry)
            assert result.eligible is True

    @patch(
        "kerkov.context.skills.skill_manager.shutil.which", return_value="/usr/bin/nmap"
    )
    def test_all_bins_present(self, mock_which):
        entry = SkillEntry(
            name="nmap-skill",
            description="Needs nmap",
            kerkov=KerkovMetadata(requires=RequiresSpec(bins=["nmap"])),
            file_path="/tmp/fake/SKILL.md",
            skill_dir="/tmp/fake",
            content="content",
        )
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SkillManager(skills_dir=tmp)
            result = mgr.check_eligibility(entry)
            assert result.eligible is True
            assert result.missing_bins == []

    @patch("kerkov.context.skills.skill_manager.shutil.which", return_value=None)
    def test_missing_bins(self, mock_which):
        entry = SkillEntry(
            name="nmap-skill",
            description="Needs nmap",
            kerkov=KerkovMetadata(requires=RequiresSpec(bins=["nmap", "r2"])),
            file_path="/tmp/fake/SKILL.md",
            skill_dir="/tmp/fake",
            content="content",
        )
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SkillManager(skills_dir=tmp)
            result = mgr.check_eligibility(entry)
            assert result.eligible is False
            assert "nmap" in result.missing_bins
            assert "r2" in result.missing_bins
            assert IneligibilityReason.MISSING_BINS in result.reasons

    @patch("kerkov.context.skills.skill_manager.is_process_elevated", return_value=True)
    def test_elevation_required_and_satisfied(self, _):
        entry = SkillEntry(
            name="vpn-skill",
            description="Needs root",
            kerkov=KerkovMetadata(requires=RequiresSpec(elevated=True)),
            file_path="/tmp/fake/SKILL.md",
            skill_dir="/tmp/fake",
            content="content",
        )
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SkillManager(skills_dir=tmp)
            result = mgr.check_eligibility(entry)
            assert result.eligible is True
            assert result.reasons == []

    @patch(
        "kerkov.context.skills.skill_manager.is_process_elevated", return_value=False
    )
    def test_elevation_required_but_not_satisfied(self, _):
        entry = SkillEntry(
            name="vpn-skill",
            description="Needs root",
            kerkov=KerkovMetadata(requires=RequiresSpec(elevated=True)),
            file_path="/tmp/fake/SKILL.md",
            skill_dir="/tmp/fake",
            content="content",
        )
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SkillManager(skills_dir=tmp)
            result = mgr.check_eligibility(entry)
            assert result.eligible is False
            assert IneligibilityReason.NEEDS_ELEVATION in result.reasons
            assert IneligibilityReason.MISSING_BINS not in result.reasons

    @patch(
        "kerkov.context.skills.skill_manager.is_process_elevated", return_value=False
    )
    @patch("kerkov.context.skills.skill_manager.shutil.which", return_value=None)
    def test_missing_bins_and_needs_elevation_both_reported(self, _which, _elev):
        entry = SkillEntry(
            name="vpn-skill",
            description="Needs root and openvpn",
            kerkov=KerkovMetadata(
                requires=RequiresSpec(bins=["openvpn"], elevated=True)
            ),
            file_path="/tmp/fake/SKILL.md",
            skill_dir="/tmp/fake",
            content="content",
        )
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SkillManager(skills_dir=tmp)
            result = mgr.check_eligibility(entry)
            assert result.eligible is False
            assert IneligibilityReason.MISSING_BINS in result.reasons
            assert IneligibilityReason.NEEDS_ELEVATION in result.reasons
            assert result.missing_bins == ["openvpn"]


class SkillManagerCatalogSummaryTest(unittest.TestCase):

    def test_empty_skills_returns_empty_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SkillManager(skills_dir=tmp)
            assert mgr.get_catalog_summary() == ""

    def test_includes_skill_in_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            _create_skill_dir(tmp, "test-skill", VALID_SKILL_MD)
            mgr = SkillManager(skills_dir=tmp)

            summary = mgr.get_catalog_summary()
            assert "SKILLS CATALOG:" in summary
            assert "test-skill" in summary
            assert "skill_lookup" in summary

    def test_excludes_model_disabled_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            _create_skill_dir(tmp, "hidden", MODEL_DISABLED_SKILL_MD)
            _create_skill_dir(tmp, "visible", MINIMAL_SKILL_MD)
            mgr = SkillManager(skills_dir=tmp)

            summary = mgr.get_catalog_summary()
            assert "hidden-skill" not in summary
            assert "minimal" in summary

    def test_one_liner_is_truncated(self):
        long_desc = "x" * 200
        skill_md = (
            "---\n"
            "name: verbose-skill\n"
            f'description: "{long_desc}"\n'
            "---\n\n# Body\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            _create_skill_dir(tmp, "verbose", skill_md)
            mgr = SkillManager(skills_dir=tmp)

            summary = mgr.get_catalog_summary()
            catalog_line = next(
                line
                for line in summary.splitlines()
                if line.startswith("- verbose-skill")
            )
            assert "…" in catalog_line
            assert long_desc not in catalog_line


if __name__ == "__main__":
    unittest.main()
