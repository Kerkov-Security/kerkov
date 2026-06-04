"""Tests for SkillTool."""

import asyncio
import tempfile
import os
import unittest
from unittest.mock import patch

from src.context.skills.skill_manager import SkillManager
from src.tools.skill_tool import SkillTool, SkillToolParams


VALID_SKILL_MD = """\
---
name: test-skill
description: "A test skill"
metadata:
  kerkov:
    category: recon/local
    tactic: discovery
    mitre: [T1046]
    difficulty: beginner
    tags: [recon, test]
    products: [nginx]
    cves: [CVE-2024-9999]
    lab_sources: [vulhub]
    requires:
      bins: [nmap]
---

# Test Skill

Instructions here.
"""

MODEL_DISABLED_SKILL_MD = """\
---
name: hidden-skill
description: "Model cannot load this"
disable-model-invocation: true
---

Hidden content.
"""

SECOND_SKILL_MD = """\
---
name: second-skill
description: "Another skill for searching"
metadata:
  kerkov:
    category: web/injection
    difficulty: intermediate
    tags: [web, sqli]
---

# Second Skill

More instructions.
"""


def _create_skill_dir(base_dir: str, dir_name: str, content: str) -> None:
    skill_dir = os.path.join(base_dir, dir_name)
    os.makedirs(skill_dir, exist_ok=True)
    with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(content)


def _build_tool_with_skills(*skill_specs: tuple[str, str]) -> SkillTool:
    """Create a SkillTool backed by a temp dir with the given skills."""
    tmp = tempfile.mkdtemp()
    for dir_name, content in skill_specs:
        _create_skill_dir(tmp, dir_name, content)
    mgr = SkillManager(skills_dir=tmp)
    return SkillTool(mgr)


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------
class SkillToolMetadataTest(unittest.TestCase):

    def test_name(self):
        tool = _build_tool_with_skills()
        assert tool.name == "skill_lookup"

    def test_params_class(self):
        tool = _build_tool_with_skills()
        assert tool.get_params_class() is SkillToolParams


# ---------------------------------------------------------------------------
# Param validation
# ---------------------------------------------------------------------------
class SkillToolParamValidationTest(unittest.TestCase):

    def setUp(self):
        self.tool = _build_tool_with_skills()

    def test_valid_list(self):
        params = SkillToolParams(action="list")
        assert self.tool.validate_params(params) is None

    def test_valid_load(self):
        params = SkillToolParams(action="load", skill_id="test")
        assert self.tool.validate_params(params) is None

    def test_valid_search_query(self):
        params = SkillToolParams(action="search", query="recon")
        assert self.tool.validate_params(params) is None

    def test_valid_search_tags(self):
        params = SkillToolParams(action="search", tags=["web"])
        assert self.tool.validate_params(params) is None

    def test_valid_search_category(self):
        params = SkillToolParams(action="search", category="recon")
        assert self.tool.validate_params(params) is None

    def test_invalid_action(self):
        params = SkillToolParams(action="delete")
        error = self.tool.validate_params(params)
        assert error is not None
        assert "Invalid action" in error

    def test_load_without_skill_id(self):
        params = SkillToolParams(action="load")
        error = self.tool.validate_params(params)
        assert error is not None
        assert "skill_id" in error

    def test_search_without_any_filter(self):
        params = SkillToolParams(action="search")
        error = self.tool.validate_params(params)
        assert error is not None
        assert "query" in error.lower() or "tags" in error.lower()


# ---------------------------------------------------------------------------
# List action
# ---------------------------------------------------------------------------
class SkillToolListTest(unittest.TestCase):

    def test_list_empty(self):
        tool = _build_tool_with_skills()
        params = SkillToolParams(action="list")
        result = asyncio.run(tool.run(params))

        assert result.success is True
        assert "No skills" in result.output

    @patch("src.context.skills.skill_manager.shutil.which", return_value=None)
    def test_list_with_skills(self, mock_which):
        tool = _build_tool_with_skills(("test-skill", VALID_SKILL_MD))
        params = SkillToolParams(action="list")
        result = asyncio.run(tool.run(params))

        assert result.success is True
        assert "test-skill" in result.output
        assert result.metadata["count"] == 1


# ---------------------------------------------------------------------------
# Load action
# ---------------------------------------------------------------------------
class SkillToolLoadTest(unittest.TestCase):

    def test_load_existing_skill(self):
        tool = _build_tool_with_skills(("test-skill", VALID_SKILL_MD))
        params = SkillToolParams(action="load", skill_id="test-skill")
        result = asyncio.run(tool.run(params))

        assert result.success is True
        assert "# Test Skill" in result.output
        assert result.metadata["skill_id"] == "test-skill"

    def test_load_nonexistent_skill(self):
        tool = _build_tool_with_skills(("test-skill", VALID_SKILL_MD))
        params = SkillToolParams(action="load", skill_id="nonexistent")
        result = asyncio.run(tool.run(params))

        assert result.success is False
        assert "not found" in result.error

    def test_load_model_disabled_skill_blocked(self):
        tool = _build_tool_with_skills(("hidden", MODEL_DISABLED_SKILL_MD))
        params = SkillToolParams(action="load", skill_id="hidden-skill")
        result = asyncio.run(tool.run(params))

        assert result.success is False
        assert "model invocation disabled" in result.error

    @patch("src.context.skills.skill_manager.shutil.which", return_value=None)
    def test_load_shows_missing_bins_warning(self, mock_which):
        tool = _build_tool_with_skills(("test-skill", VALID_SKILL_MD))
        params = SkillToolParams(action="load", skill_id="test-skill")
        result = asyncio.run(tool.run(params))

        assert result.success is True
        assert "Missing required binaries" in result.output
        assert result.metadata["eligible"] is False

    def test_load_includes_skill_location_header(self):
        tool = _build_tool_with_skills(("test-skill", VALID_SKILL_MD))
        entry = tool._skill_manager.get_skill("test-skill")
        assert entry is not None

        params = SkillToolParams(action="load", skill_id="test-skill")
        result = asyncio.run(tool.run(params))

        assert result.success is True
        assert "**Location:**" in result.output
        assert entry.skill_dir in result.output
        assert "scripts/" in result.output


# ---------------------------------------------------------------------------
# Search action
# ---------------------------------------------------------------------------
class SkillToolSearchTest(unittest.TestCase):

    def test_search_with_match(self):
        tool = _build_tool_with_skills(
            ("test-skill", VALID_SKILL_MD),
            ("second", SECOND_SKILL_MD),
        )
        params = SkillToolParams(action="search", query="recon")
        result = asyncio.run(tool.run(params))

        assert result.success is True
        assert "test-skill" in result.output

    def test_search_no_match(self):
        tool = _build_tool_with_skills(("test-skill", VALID_SKILL_MD))
        params = SkillToolParams(action="search", query="zzzznotfound")
        result = asyncio.run(tool.run(params))

        assert result.success is True
        assert "No skills matched" in result.output

    def test_search_by_tags(self):
        tool = _build_tool_with_skills(
            ("test-skill", VALID_SKILL_MD),
            ("second", SECOND_SKILL_MD),
        )
        params = SkillToolParams(action="search", tags=["sqli"])
        result = asyncio.run(tool.run(params))

        assert result.success is True
        assert "second-skill" in result.output

    def test_search_by_category(self):
        tool = _build_tool_with_skills(
            ("test-skill", VALID_SKILL_MD),
            ("second", SECOND_SKILL_MD),
        )
        params = SkillToolParams(action="search", category="web")
        result = asyncio.run(tool.run(params))

        assert result.success is True
        assert "second-skill" in result.output

    def test_search_by_extended_metadata_via_query(self):
        tool = _build_tool_with_skills(("test-skill", VALID_SKILL_MD))
        params = SkillToolParams(action="search", query="CVE-2024-9999")
        result = asyncio.run(tool.run(params))

        assert result.success is True
        assert "test-skill" in result.output


if __name__ == "__main__":
    unittest.main()
