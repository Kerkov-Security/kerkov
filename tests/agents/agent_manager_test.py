"""Tests for AgentManager — profile loading and indexing."""

import tempfile
import unittest
from pathlib import Path

from kerkov.agents.agent_manager import AgentManager
from kerkov.agents.base_agent import BUILTIN_PROFILES
from kerkov.agents.models import AgentProfile


def _write_agent(dir_path: Path, slug: str, content: str) -> Path:
    sub = dir_path / slug
    sub.mkdir(parents=True, exist_ok=True)
    f = sub / "AGENT.md"
    f.write_text(content, encoding="utf-8")
    return f


class BuiltinsTest(unittest.TestCase):
    def test_all_builtins_loaded_with_empty_user_dir(self):
        with tempfile.TemporaryDirectory() as d:
            mgr = AgentManager(agents_dir=d)
        names = {p.name for p in mgr.list_profiles()}
        self.assertEqual(set(BUILTIN_PROFILES.keys()), names)

    def test_get_profile_returns_builtin(self):
        with tempfile.TemporaryDirectory() as d:
            mgr = AgentManager(agents_dir=d)
        profile = mgr.get_profile("recon")
        self.assertIsNotNone(profile)
        self.assertEqual(profile.source, "builtin")
        self.assertIn("read", profile.allowed_tools)
        self.assertNotIn("write", profile.allowed_tools)

    def test_analysis_profile_is_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            mgr = AgentManager(agents_dir=d)
        profile = mgr.get_profile("analysis")
        self.assertIsNotNone(profile)
        for forbidden in ("write", "exec", "process", "http"):
            self.assertNotIn(forbidden, profile.allowed_tools)

    def test_read_only_profiles_can_delegate_supply_chain_analysis(self):
        with tempfile.TemporaryDirectory() as d:
            mgr = AgentManager(agents_dir=d)
        for profile_name in ("recon", "analysis"):
            with self.subTest(profile=profile_name):
                profile = mgr.get_profile(profile_name)
                self.assertIn("sbom_generate", profile.allowed_tools)
                self.assertIn("vulnerability_scan", profile.allowed_tools)

    def test_every_builtin_can_recall(self):
        # `recall` is only granted through the whitelist, so a profile that
        # omits it sends the agent off to re-run work the store already has.
        with tempfile.TemporaryDirectory() as d:
            mgr = AgentManager(agents_dir=d)
        for name in BUILTIN_PROFILES:
            with self.subTest(profile=name):
                self.assertIn("recall", mgr.get_profile(name).allowed_tools)

    def test_container_operator_has_narrow_lifecycle_toolset(self):
        with tempfile.TemporaryDirectory() as d:
            mgr = AgentManager(agents_dir=d)
        profile = mgr.get_profile("container-operator")
        self.assertIsNotNone(profile)
        self.assertIn("container_lifecycle", profile.allowed_tools)
        self.assertIn("exec", profile.allowed_tools)
        self.assertNotIn("process", profile.allowed_tools)
        self.assertNotIn("http", profile.allowed_tools)
        self.assertIn("Never run `docker compose up`", profile.system_prompt)


class UserDefinedTest(unittest.TestCase):
    def test_default_dir_loads_top_level_agents(self):
        mgr = AgentManager()

        profile = mgr.get_profile("example-recon")
        self.assertIsNotNone(profile)
        self.assertNotEqual(profile.source, "builtin")
        self.assertIn("skill_lookup", profile.allowed_tools)

    def test_valid_agent_md_is_loaded(self):
        with tempfile.TemporaryDirectory() as d:
            _write_agent(
                Path(d),
                "my-agent",
                """---
name: my-agent
description: A custom user agent
allowed_tools:
  - read
  - http
max_iterations: 15
---

You are my-agent. Do useful things.
""",
            )
            mgr = AgentManager(agents_dir=d)
            profile = mgr.get_profile("my-agent")
        self.assertIsNotNone(profile)
        self.assertEqual(profile.allowed_tools, ["read", "http"])
        self.assertEqual(profile.max_iterations, 15)
        self.assertIn("my-agent", profile.system_prompt)
        self.assertNotEqual(profile.source, "builtin")

    def test_user_defined_can_shadow_builtin(self):
        with tempfile.TemporaryDirectory() as d:
            _write_agent(
                Path(d),
                "recon",
                """---
name: recon
description: Custom recon
allowed_tools:
  - read
---

custom recon prompt
""",
            )
            mgr = AgentManager(agents_dir=d)
            profile = mgr.get_profile("recon")
        self.assertEqual(profile.allowed_tools, ["read"])
        self.assertNotEqual(profile.source, "builtin")

    def test_missing_required_fields_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            _write_agent(
                Path(d),
                "broken",
                """---
name: broken
---

no description, no allowed_tools
""",
            )
            mgr = AgentManager(agents_dir=d)
        self.assertIsNone(mgr.get_profile("broken"))

    def test_non_yaml_frontmatter_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            _write_agent(
                Path(d),
                "noyaml",
                "no frontmatter at all\n",
            )
            mgr = AgentManager(agents_dir=d)
        self.assertIsNone(mgr.get_profile("noyaml"))


class EffectiveToolsTest(unittest.TestCase):
    def test_none_request_returns_full_whitelist(self):
        profile = AgentProfile(
            name="x",
            description="d",
            system_prompt="p",
            allowed_tools=["read", "http"],
        )
        self.assertEqual(profile.effective_tools(None), ["read", "http"])

    def test_subset_request_is_intersected(self):
        profile = AgentProfile(
            name="x",
            description="d",
            system_prompt="p",
            allowed_tools=["read", "http", "exec"],
        )
        self.assertEqual(profile.effective_tools(["read", "exec"]), ["read", "exec"])

    def test_widening_attempt_is_dropped(self):
        profile = AgentProfile(
            name="x",
            description="d",
            system_prompt="p",
            allowed_tools=["read"],
        )
        # Requesting `write` (not in the whitelist) is silently dropped — the
        # profile's whitelist is the hard ceiling.
        self.assertEqual(profile.effective_tools(["read", "write"]), ["read"])

    def test_request_outside_whitelist_returns_empty(self):
        profile = AgentProfile(
            name="x",
            description="d",
            system_prompt="p",
            allowed_tools=["read"],
        )
        self.assertEqual(profile.effective_tools(["write"]), [])


class IndexSummaryTest(unittest.TestCase):
    def test_summary_includes_all_builtins(self):
        with tempfile.TemporaryDirectory() as d:
            mgr = AgentManager(agents_dir=d)
        summary = mgr.get_index_summary()
        for name in BUILTIN_PROFILES:
            self.assertIn(name, summary)
        self.assertIn("AGENT PROFILES", summary)


if __name__ == "__main__":
    unittest.main()
