"""Tests for SkillManager."""

import os
import tempfile
import unittest
from unittest.mock import patch

from src.context.skills.models import (
    IneligibilityReason,
    KerkovMetadata,
    RequiresSpec,
    SkillEntry,
)
from src.context.skills.skill_manager import SkillManager


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


def _create_skill_dir(base_dir: str, skill_name: str, content: str) -> str:
    """Create a skill directory with a SKILL.md file."""
    skill_dir = os.path.join(base_dir, skill_name)
    os.makedirs(skill_dir, exist_ok=True)
    skill_file = os.path.join(skill_dir, "SKILL.md")
    with open(skill_file, "w", encoding="utf-8") as f:
        f.write(content)
    return skill_dir


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------
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
        fm, content = SkillManager._split_frontmatter(FRONTMATTER_WITH_DASHES_IN_YAML_MD)
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


# ---------------------------------------------------------------------------
# Skill discovery and parsing
# ---------------------------------------------------------------------------
class SkillManagerScanTest(unittest.TestCase):

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


# ---------------------------------------------------------------------------
# Skill lookup
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
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

    def test_search_by_partial_name(self):
        results = self.mgr.search(query="test")
        assert any(e.name == "test-skill" for e in results)

    def test_search_by_description(self):
        results = self.mgr.search(query="unit tests")
        assert any(e.name == "test-skill" for e in results)

    def test_search_by_tag(self):
        results = self.mgr.search(tags=["recon"])
        assert any(e.name == "test-skill" for e in results)

    def test_search_by_category(self):
        results = self.mgr.search(category="recon")
        assert any(e.name == "test-skill" for e in results)

    def test_search_by_difficulty(self):
        results = self.mgr.search(difficulty="beginner")
        assert any(e.name == "test-skill" for e in results)

    def test_search_no_match(self):
        results = self.mgr.search(query="zzzznonexistent")
        assert len(results) == 0

    def test_search_no_filters_returns_empty(self):
        results = self.mgr.search()
        assert len(results) == 0

    def test_exact_name_ranks_highest(self):
        results = self.mgr.search(query="test-skill")
        # Exact match should be first
        assert results[0].name == "test-skill"

    def test_query_matches_tag(self):
        results = self.mgr.search(query="recon")
        assert any(e.name == "test-skill" for e in results)

    def test_query_matches_extended_metadata(self):
        for query in ("nginx", "CVE-2024-9999", "T1595", "discovery", "vulhub"):
            results = self.mgr.search(query=query)
            assert any(e.name == "test-skill" for e in results)


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------
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

    @patch("src.context.skills.skill_manager.shutil.which", return_value="/usr/bin/nmap")
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

    @patch("src.context.skills.skill_manager.shutil.which", return_value=None)
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

    @patch("src.context.skills.skill_manager.is_process_elevated", return_value=True)
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

    @patch("src.context.skills.skill_manager.is_process_elevated", return_value=False)
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

    @patch("src.context.skills.skill_manager.is_process_elevated", return_value=False)
    @patch("src.context.skills.skill_manager.shutil.which", return_value=None)
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


# ---------------------------------------------------------------------------
# Index summary
# ---------------------------------------------------------------------------
class SkillManagerIndexSummaryTest(unittest.TestCase):

    def test_empty_skills_returns_empty_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SkillManager(skills_dir=tmp)
            assert mgr.get_index_summary() == ""

    def test_includes_skill_in_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            _create_skill_dir(tmp, "test-skill", VALID_SKILL_MD)
            mgr = SkillManager(skills_dir=tmp)

            summary = mgr.get_index_summary()
            assert "SKILLS DATABASE:" in summary
            assert "test-skill" in summary
            assert "skill_lookup" in summary

    def test_excludes_model_disabled_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            _create_skill_dir(tmp, "hidden", MODEL_DISABLED_SKILL_MD)
            _create_skill_dir(tmp, "visible", MINIMAL_SKILL_MD)
            mgr = SkillManager(skills_dir=tmp)

            summary = mgr.get_index_summary()
            assert "hidden-skill" not in summary
            assert "minimal" in summary


if __name__ == "__main__":
    unittest.main()
