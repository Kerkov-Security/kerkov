"""Tests for RecallTool — the read path over the engagement knowledge base."""

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Optional

from kerkov.core.knowledge_base.engagement_kb import EngagementKnowledgeBase
from kerkov.tools.recall_tool import MAX_LIMIT, RecallTool

NMAP_OUTPUT = """PORT     STATE SERVICE VERSION
22/tcp   open  ssh     OpenSSH 8.2p1
8080/tcp open  http    Apache Tomcat 9.0.30
"""

SMB_OUTPUT = "SMB signing is disabled on 10.10.10.7 (relay is possible)"


def _run(coro):
    return asyncio.run(coro)


def _create_mock_getter(kb: Optional[EngagementKnowledgeBase]):
    """Late-bound knowledge base getter, as the CLI supplies."""
    return lambda: kb


class _RecallTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.kb = EngagementKnowledgeBase("root-1", self.tmpdir)
        self.tool = RecallTool(_create_mock_getter(self.kb))

    def tearDown(self) -> None:
        self.kb.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _add(self, **kwargs) -> int:
        kwargs.setdefault("engagement_id", "root-1")
        return _run(self.kb.add_artifact(**kwargs))

    def _execute(self, **params):
        return _run(self.tool.execute(params))


class SnippetSearchTest(_RecallTestCase):
    def test_returns_ranked_snippets_with_source_and_id(self):
        artifact_id = self._add(
            body=NMAP_OUTPUT, source="exec", title="nmap 10.10.10.5"
        )

        result = self._execute(query="port 8080")

        self.assertTrue(result.success)
        self.assertIn(f"artifact#{artifact_id}", result.output)
        self.assertIn("nmap 10.10.10.5", result.output)
        self.assertIn("via exec", result.output)
        self.assertEqual(result.metadata["count"], 1)
        self.assertEqual(result.metadata["hits"][0]["artifact_id"], artifact_id)

    def test_snippet_mode_does_not_dump_the_whole_body(self):
        self._add(body="HEADER\n" + ("filler line\n" * 500) + "TRAILER", source="exec")

        result = self._execute(query="filler")

        self.assertTrue(result.success)
        self.assertNotIn("TRAILER", result.output)
        self.assertIn("Expand any of these", result.output)

    def test_full_returns_complete_bodies(self):
        self._add(body=NMAP_OUTPUT, source="exec")

        result = self._execute(query="tomcat", full=True)

        self.assertIn("OpenSSH 8.2p1", result.output)
        self.assertIn("Apache Tomcat 9.0.30", result.output)
        self.assertNotIn("Expand any of these", result.output)

    def test_limit_is_capped(self):
        for i in range(MAX_LIMIT + 15):
            self._add(body=f"tomcat host {i}", source="exec")

        result = self._execute(query="tomcat", limit=999)

        self.assertEqual(result.metadata["count"], MAX_LIMIT)

    def test_kind_and_source_filters_are_passed_through(self):
        self._add(body="tomcat via http", source="http://a", kind="http_response")
        exec_id = self._add(body="tomcat via exec", source="exec", kind="tool_output")

        result = self._execute(query="tomcat", kind="tool_output", source="exec")

        self.assertEqual(result.metadata["count"], 1)
        self.assertEqual(result.metadata["hits"][0]["artifact_id"], exec_id)


class ArtifactExpansionTest(_RecallTestCase):
    def test_artifact_reference_query_returns_the_full_body(self):
        artifact_id = self._add(body=NMAP_OUTPUT, source="exec", title="nmap")

        result = self._execute(query=f"artifact#{artifact_id}")

        self.assertTrue(result.success)
        self.assertIn("OpenSSH 8.2p1", result.output)
        self.assertEqual(result.metadata["artifact_id"], artifact_id)

    def test_artifact_reference_is_recognized_inside_a_sentence(self):
        artifact_id = self._add(body=NMAP_OUTPUT, source="exec")

        result = self._execute(query=f"show me artifact#{artifact_id} please")

        self.assertTrue(result.success)
        self.assertIn("OpenSSH 8.2p1", result.output)

    def test_unknown_artifact_reference_fails_clearly(self):
        result = self._execute(query="artifact#4242")

        self.assertFalse(result.success)
        self.assertIn("4242", result.error)


class EngagementScopeTest(_RecallTestCase):
    def test_defaults_to_the_whole_engagement_tree(self):
        self._add(body=NMAP_OUTPUT, source="exec")
        self._add(body=SMB_OUTPUT, source="exec", engagement_id="agent-a")

        result = self._execute(query="tomcat OR signing")

        engagements = {h["engagement_id"] for h in result.metadata["hits"]}
        self.assertEqual(engagements, {"root-1", "agent-a"})

    def test_engagement_filter_drills_into_one_agent(self):
        self._add(body=NMAP_OUTPUT, source="exec")
        self._add(
            body=SMB_OUTPUT,
            source="exec",
            engagement_id="agent-a",
            agent_type="recon",
        )

        result = self._execute(query="signing", engagement="agent-a")

        self.assertEqual(result.metadata["count"], 1)
        self.assertEqual(result.metadata["hits"][0]["engagement_id"], "agent-a")
        self.assertIn("recon", result.output)

    def test_empty_engagement_is_reported_differently_from_no_match(self):
        # These are different facts: the second should send the model back
        # with another query, the first should not.
        self._add(body=NMAP_OUTPUT, source="exec")

        never_wrote = self._execute(query="anything", engagement="agent-ghost")
        no_match = self._execute(query="kerberoasting")

        self.assertTrue(never_wrote.success)
        self.assertIn(
            "No artifacts recorded for engagement 'agent-ghost'", never_wrote.output
        )
        self.assertEqual(never_wrote.metadata["stored_in_scope"], 0)

        self.assertTrue(no_match.success)
        self.assertIn("No stored artifact matches", no_match.output)
        self.assertEqual(no_match.metadata["stored_in_scope"], 1)

    def test_empty_engagement_message_lists_engagements_that_do_have_artifacts(self):
        self._add(body=SMB_OUTPUT, engagement_id="agent-a")

        result = self._execute(query="anything", engagement="agent-ghost")

        self.assertIn("agent-a", result.output)


class ValidationTest(_RecallTestCase):
    def test_blank_query_is_rejected(self):
        result = self._execute(query="   ")

        self.assertFalse(result.success)
        self.assertIn("query is required", result.error)

    def test_zero_limit_is_rejected(self):
        result = self._execute(query="tomcat", limit=0)

        self.assertFalse(result.success)
        self.assertIn("limit must be at least 1", result.error)

    def test_unknown_kind_is_rejected_rather_than_matching_nothing(self):
        self._add(body=NMAP_OUTPUT, source="exec")

        result = self._execute(query="tomcat", kind="credential")

        self.assertFalse(result.success)
        self.assertIn("kind", result.error)

    def test_query_of_pure_punctuation_returns_no_match_not_a_crash(self):
        self._add(body=NMAP_OUTPUT, source="exec")

        result = self._execute(query="???")

        self.assertTrue(result.success)
        self.assertEqual(result.metadata["count"], 0)


class NoKnowledgeBaseTest(unittest.TestCase):
    def test_reports_cleanly_when_no_engagement_is_active(self):
        tool = RecallTool(_create_mock_getter(None))

        result = _run(tool.execute({"query": "tomcat"}))

        self.assertFalse(result.success)
        self.assertIn("No knowledge base is active", result.error)


class SchemaTest(_RecallTestCase):
    def test_schema_documents_the_agent_drilldown(self):
        schema = self.tool.get_generic_schema()

        self.assertEqual(schema.name, "recall")
        self.assertIn("engagement", schema.parameters["properties"])
        self.assertIn("delegated agent", self.tool.description)


if __name__ == "__main__":
    unittest.main()
