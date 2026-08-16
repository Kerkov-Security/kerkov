"""Tests for the engagement knowledge base — FTS5 store, filters, lifecycle."""

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from kerkov.core.knowledge_base.embeddings import (
    EmbeddingBackend,
    chunk_text,
    cosine_similarity,
    pack_vector,
    unpack_vector,
)
from kerkov.core.knowledge_base.engagement_kb import (
    EngagementKnowledgeBase,
    build_match_query,
    delete_kb_for_root,
    kb_path_for,
    resolve_root_engagement_id,
)
from kerkov.core.knowledge_base.models import ArtifactKind

NMAP_OUTPUT = """PORT     STATE SERVICE VERSION
22/tcp   open  ssh     OpenSSH 8.2p1
8080/tcp open  http    Apache Tomcat 9.0.30
"""

SMB_OUTPUT = "SMB signing is disabled on 10.10.10.7 (relay is possible)"


def _run(coro):
    return asyncio.run(coro)


class _FakeEmbeddingBackend(EmbeddingBackend):
    """Deterministic offline embeddings over a handful of concept axes.

    Each axis is a set of related terms, so text scores on a concept without
    having to contain the query's exact words. That is what makes the semantic
    path distinguishable from the keyword path in a test: a document can be a
    vector match while being invisible to FTS5.
    """

    CONCEPTS = (
        ("smb_relay", ("smb", "signing", "relay", "ntlm")),
        ("credentials", ("password", "creds", "credential", "login")),
        ("web", ("http", "tomcat", "apache")),
    )

    def __init__(self) -> None:
        self.calls: List[Sequence[str]] = []

    async def embed(self, texts: Sequence[str]) -> List[List[float]]:
        self.calls.append(list(texts))
        vectors = []
        for text in texts:
            lowered = text.lower()
            vectors.append(
                [
                    float(sum(lowered.count(term) for term in terms))
                    for _, terms in self.CONCEPTS
                ]
            )
        return vectors


def _create_mock_parent_map(mapping: Dict[str, Optional[str]]):
    """Parent lookup backed by a dict, standing in for EngagementStore."""

    def parent_of(engagement_id: str) -> Optional[str]:
        return mapping.get(engagement_id)

    return parent_of


class _KbTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.kb = EngagementKnowledgeBase("root-1", self.tmpdir)

    def tearDown(self) -> None:
        self.kb.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _add(self, **kwargs) -> int:
        kwargs.setdefault("engagement_id", "root-1")
        return _run(self.kb.add_artifact(**kwargs))


class IngestionRoundtripTest(_KbTestCase):
    def test_add_then_recall_returns_the_hit_with_source_and_metadata(self):
        artifact_id = self._add(
            body=NMAP_OUTPUT,
            source="exec",
            title="nmap 10.10.10.5",
            metadata={"host": "10.10.10.5"},
        )

        hits = _run(self.kb.search("port 8080"))

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].artifact_id, artifact_id)
        self.assertEqual(hits[0].source, "exec")
        self.assertEqual(hits[0].title, "nmap 10.10.10.5")
        self.assertEqual(hits[0].metadata["host"], "10.10.10.5")
        self.assertIn("8080", hits[0].snippet)

    def test_snippet_only_until_full_is_requested(self):
        self._add(body=NMAP_OUTPUT, source="exec")

        snippet_hit = _run(self.kb.search("tomcat"))[0]
        full_hit = _run(self.kb.search("tomcat", full=True))[0]

        self.assertIsNone(snippet_hit.body)
        self.assertEqual(full_hit.body, NMAP_OUTPUT)

    def test_size_bytes_records_the_encoded_length(self):
        body = "café " * 100
        artifact_id = self._add(body=body, source="exec")

        hit = _run(self.kb.get_artifact(artifact_id))

        self.assertEqual(hit.size_bytes, len(body.encode("utf-8")))


class KeywordSearchTest(_KbTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.nmap_id = self._add(body=NMAP_OUTPUT, source="exec", title="nmap scan")
        self.smb_id = self._add(
            body=SMB_OUTPUT,
            source="exec",
            title="smb audit",
            kind=ArtifactKind.FILE,
            engagement_id="agent-a",
            agent_type="recon",
        )
        # Shaped the way the ingestion hook writes it: the tool name is the
        # source, and the target only appears in the title.
        self.http_id = self._add(
            body="HTTP/1.1 200 OK\nServer: Apache Tomcat 9.0.30",
            source="http",
            title="http: http://10.10.10.5:8080/manager",
            kind=ArtifactKind.HTTP_RESPONSE,
        )

    def test_multi_term_query_requires_all_terms_when_possible(self):
        hits = _run(self.kb.search("smb signing"))

        self.assertEqual([h.artifact_id for h in hits], [self.smb_id])

    def test_falls_back_to_any_term_when_all_terms_match_nothing(self):
        # "tomcat" and "signing" never co-occur; an AND-only search would
        # report nothing rather than the two artifacts that each match one.
        hits = _run(self.kb.search("tomcat signing"))

        self.assertEqual(
            sorted(h.artifact_id for h in hits),
            sorted([self.nmap_id, self.smb_id, self.http_id]),
        )

    def test_kind_filter(self):
        hits = _run(self.kb.search("tomcat", kind=ArtifactKind.HTTP_RESPONSE))

        self.assertEqual([h.artifact_id for h in hits], [self.http_id])

    def test_source_filter_matches_the_producing_tool(self):
        hits = _run(self.kb.search("tomcat", source="http"))

        self.assertEqual([h.artifact_id for h in hits], [self.http_id])

    def test_source_filter_matches_the_target_in_the_title(self):
        # The host never reaches the source column, so a host filter has to
        # match the title or it silently returns nothing.
        hits = _run(self.kb.search("tomcat", source="10.10.10.5"))

        self.assertEqual([h.artifact_id for h in hits], [self.http_id])

    def test_source_filter_excludes_non_matching_artifacts(self):
        hits = _run(self.kb.search("tomcat", source="192.168.1.1"))

        self.assertEqual(hits, [])

    def test_engagement_filter_scopes_to_one_run(self):
        in_agent = _run(self.kb.search("smb", engagement_id="agent-a"))
        in_master = _run(self.kb.search("smb", engagement_id="root-1"))

        self.assertEqual([h.artifact_id for h in in_agent], [self.smb_id])
        self.assertEqual(in_master, [])

    def test_search_spans_the_whole_tree_by_default(self):
        hits = _run(self.kb.search("tomcat OR signing"))

        engagements = {h.engagement_id for h in hits}
        self.assertEqual(engagements, {"root-1", "agent-a"})

    def test_agent_type_is_returned_so_hits_can_be_attributed(self):
        hit = _run(self.kb.search("smb", engagement_id="agent-a"))[0]

        self.assertEqual(hit.agent_type, "recon")

    def test_limit_is_respected(self):
        for i in range(10):
            self._add(body=f"tomcat instance {i}", source="exec")

        hits = _run(self.kb.search("tomcat", limit=3))

        self.assertEqual(len(hits), 3)


class MatchQueryTest(unittest.TestCase):
    def test_punctuation_from_natural_language_is_neutralized(self):
        # Raw, these would be FTS5 syntax errors or operators.
        for raw in ("what's on 10.0.0.5?", 'a "quoted" thing', "creds (admin)", "a OR"):
            with self.subTest(raw=raw):
                match = build_match_query(raw)
                self.assertIsNotNone(match)
                self.assertNotIn("?", match)

    def test_dotted_forms_survive_as_a_single_phrase(self):
        self.assertEqual(build_match_query("10.0.0.5"), '"10.0.0.5"')

    def test_returns_none_when_there_is_nothing_to_search_for(self):
        self.assertIsNone(build_match_query("!!! ???"))
        self.assertIsNone(build_match_query(""))

    def test_operator_selects_and_or_or(self):
        self.assertEqual(build_match_query("a b", "AND"), '"a" AND "b"')
        self.assertEqual(build_match_query("a b", "OR"), '"a" OR "b"')


class ArtifactExpansionTest(_KbTestCase):
    def test_get_artifact_returns_the_complete_body(self):
        artifact_id = self._add(body=NMAP_OUTPUT, source="exec")

        hit = _run(self.kb.get_artifact(artifact_id))

        self.assertEqual(hit.body, NMAP_OUTPUT)
        self.assertEqual(hit.artifact_id, artifact_id)

    def test_unknown_id_returns_none(self):
        self.assertIsNone(_run(self.kb.get_artifact(999)))


class CountingTest(_KbTestCase):
    def test_count_distinguishes_empty_scope_from_no_match(self):
        self._add(body=NMAP_OUTPUT, engagement_id="root-1")

        self.assertEqual(_run(self.kb.count()), 1)
        self.assertEqual(_run(self.kb.count("root-1")), 1)
        self.assertEqual(_run(self.kb.count("agent-never-wrote")), 0)

    def test_engagement_ids_lists_contributors(self):
        self._add(body="a", engagement_id="root-1")
        self._add(body="b", engagement_id="agent-a")

        self.assertEqual(set(_run(self.kb.engagement_ids())), {"root-1", "agent-a"})


class RootResolutionTest(unittest.TestCase):
    def test_root_resolves_to_itself(self):
        parent_of = _create_mock_parent_map({"root-1": None})

        self.assertEqual(resolve_root_engagement_id("root-1", parent_of), "root-1")

    def test_agent_resolves_to_its_master(self):
        parent_of = _create_mock_parent_map({"agent-a": "root-1", "root-1": None})

        self.assertEqual(resolve_root_engagement_id("agent-a", parent_of), "root-1")

    def test_multi_level_chain_resolves_to_the_root(self):
        # Today's spawn wiring records the master as every agent's parent, so
        # this chain does not arise in practice — the walk is a loop precisely
        # so a future fix to that parentage keeps resolving to the root.
        parent_of = _create_mock_parent_map(
            {"depth-2": "depth-1", "depth-1": "root-1", "root-1": None}
        )

        self.assertEqual(resolve_root_engagement_id("depth-2", parent_of), "root-1")

    def test_cycle_does_not_hang(self):
        parent_of = _create_mock_parent_map({"a": "b", "b": "a"})

        self.assertIn(resolve_root_engagement_id("a", parent_of), {"a", "b"})

    def test_missing_parent_metadata_is_treated_as_root(self):
        parent_of = _create_mock_parent_map({})

        self.assertEqual(resolve_root_engagement_id("orphan", parent_of), "orphan")


class LifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_reopening_the_same_root_sees_the_same_artifacts(self):
        kb = EngagementKnowledgeBase("root-1", self.tmpdir)
        _run(kb.add_artifact(engagement_id="root-1", body=NMAP_OUTPUT))
        kb.close()

        reopened = EngagementKnowledgeBase("root-1", self.tmpdir)
        try:
            hits = _run(reopened.search("tomcat"))
            self.assertEqual(len(hits), 1)
        finally:
            reopened.close()

    def test_file_is_named_for_the_root_not_the_writing_engagement(self):
        kb = EngagementKnowledgeBase("root-1", self.tmpdir)
        try:
            _run(kb.add_artifact(engagement_id="agent-a", body=SMB_OUTPUT))
        finally:
            kb.close()

        self.assertTrue(kb_path_for(self.tmpdir, "root-1").exists())
        self.assertFalse(kb_path_for(self.tmpdir, "agent-a").exists())

    def test_delete_engagement_rows_leaves_the_rest_of_the_tree(self):
        kb = EngagementKnowledgeBase("root-1", self.tmpdir)
        try:
            _run(kb.add_artifact(engagement_id="root-1", body=NMAP_OUTPUT))
            _run(kb.add_artifact(engagement_id="agent-a", body=SMB_OUTPUT))

            removed = kb.delete_engagement_rows("agent-a")

            self.assertEqual(removed, 1)
            self.assertEqual(_run(kb.count()), 1)
            # The FTS index must drop with the row, not outlive it.
            self.assertEqual(_run(kb.search("signing")), [])
            self.assertEqual(len(_run(kb.search("tomcat"))), 1)
        finally:
            kb.close()

    def test_delete_kb_for_root_removes_the_file(self):
        kb = EngagementKnowledgeBase("root-1", self.tmpdir)
        _run(kb.add_artifact(engagement_id="root-1", body=NMAP_OUTPUT))
        kb.close()

        self.assertTrue(delete_kb_for_root(self.tmpdir, "root-1"))
        self.assertFalse(kb_path_for(self.tmpdir, "root-1").exists())

    def test_delete_kb_for_root_is_a_noop_when_absent(self):
        self.assertFalse(delete_kb_for_root(self.tmpdir, "never-existed"))

    def test_created_lazily_no_file_until_first_use(self):
        EngagementKnowledgeBase("root-lazy", self.tmpdir)

        self.assertFalse(kb_path_for(self.tmpdir, "root-lazy").exists())


class ConcurrentWriteTest(_KbTestCase):
    def test_gathered_sibling_agents_all_land_in_the_one_store(self):
        # A delegated batch runs its agents through asyncio.gather, so several
        # engagements write to this single file at once.
        async def scenario():
            await asyncio.gather(
                *[
                    self.kb.add_artifact(
                        engagement_id=f"agent-{i}",
                        body=f"agent {i} found tomcat on host {i}",
                        source="exec",
                    )
                    for i in range(12)
                ]
            )
            return await self.kb.count()

        self.assertEqual(_run(scenario()), 12)
        self.assertEqual(len(_run(self.kb.search("tomcat", limit=20))), 12)


class EmbeddingsDisabledTest(_KbTestCase):
    def test_no_backend_means_no_vectors_and_no_embed_calls(self):
        self._add(body=NMAP_OUTPUT, source="exec")
        _run(self.kb.search("tomcat"))

        conn = self.kb._connect()
        rows = conn.execute("SELECT COUNT(*) FROM artifact_embeddings").fetchone()

        self.assertIsNone(self.kb._embedding_backend)
        self.assertEqual(rows[0], 0)


class EmbeddingsEnabledTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.backend = _FakeEmbeddingBackend()
        self.kb = EngagementKnowledgeBase("root-1", self.tmpdir, self.backend)

    def tearDown(self) -> None:
        self.kb.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_vectors_are_stored_on_write(self):
        _run(self.kb.add_artifact(engagement_id="root-1", body=SMB_OUTPUT))

        conn = self.kb._connect()
        count = conn.execute("SELECT COUNT(*) FROM artifact_embeddings").fetchone()[0]

        self.assertGreater(count, 0)
        self.assertTrue(self.backend.calls)

    def test_semantic_hits_blend_with_keyword_hits(self):
        keyword_id = _run(
            self.kb.add_artifact(engagement_id="root-1", body="ntlm auth notes")
        )
        # Shares no term with the query, so FTS5 cannot reach it — only the
        # vector pass can.
        semantic_id = _run(
            self.kb.add_artifact(engagement_id="root-1", body=SMB_OUTPUT)
        )

        hits = _run(self.kb.search("ntlm"))

        ids = [h.artifact_id for h in hits]
        # The keyword match ranks first; the vector match is appended, not
        # allowed to displace it.
        self.assertEqual(ids[0], keyword_id)
        self.assertIn(semantic_id, ids)
        self.assertIn("semantic_score", hits[-1].metadata)

    def test_a_purely_semantic_match_is_unreachable_without_embeddings(self):
        # Guards the premise of the test above: the blended hit really is one
        # the keyword index could not produce on its own.
        plain = EngagementKnowledgeBase("root-plain", self.tmpdir)
        try:
            _run(plain.add_artifact(engagement_id="root-plain", body=SMB_OUTPUT))

            self.assertEqual(_run(plain.search("ntlm")), [])
        finally:
            plain.close()

    def test_a_failing_backend_does_not_lose_the_artifact(self):
        class _BrokenBackend(EmbeddingBackend):
            async def embed(self, texts):
                raise RuntimeError("provider down")

        kb = EngagementKnowledgeBase("root-2", self.tmpdir, _BrokenBackend())
        try:
            artifact_id = _run(
                kb.add_artifact(engagement_id="root-2", body=NMAP_OUTPUT)
            )

            self.assertGreater(artifact_id, 0)
            self.assertEqual(_run(kb.count()), 1)
        finally:
            kb.close()


class EmbeddingHelpersTest(unittest.TestCase):
    def test_pack_roundtrip(self):
        vector = [0.5, -1.25, 3.0]

        restored = unpack_vector(pack_vector(vector))

        self.assertEqual(len(restored), 3)
        for original, value in zip(vector, restored):
            self.assertAlmostEqual(original, value, places=5)

    def test_cosine_similarity_bounds(self):
        self.assertAlmostEqual(cosine_similarity([1.0, 0.0], [1.0, 0.0]), 1.0)
        self.assertAlmostEqual(cosine_similarity([1.0, 0.0], [0.0, 1.0]), 0.0)
        self.assertEqual(cosine_similarity([0.0, 0.0], [1.0, 1.0]), 0.0)
        self.assertEqual(cosine_similarity([1.0], [1.0, 1.0]), 0.0)

    def test_chunking_prefers_line_boundaries(self):
        text = "\n".join(f"line {i}" for i in range(100))

        chunks = chunk_text(text, chunk_chars=50)

        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks).replace("\n", ""), text.replace("\n", ""))

    def test_chunking_handles_a_single_oversized_line(self):
        chunks = chunk_text("x" * 250, chunk_chars=100)

        self.assertEqual(len(chunks), 3)


if __name__ == "__main__":
    unittest.main()
