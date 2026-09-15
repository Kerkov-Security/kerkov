"""Tests for SkillRetriever."""

import unittest

from kerkov.context.skills.models import KerkovMetadata, SkillEntry
from kerkov.context.skills.skill_retriever import (
    SkillRetriever,
    expand_query,
)


def _create_mock_skill(
    name: str,
    description: str = "",
    *,
    tags=None,
    category=None,
    tactic=None,
    mitre=None,
    products=None,
    vuln_classes=None,
    cves=None,
    difficulty=None,
    platforms=None,
    lab_sources=None,
) -> SkillEntry:
    """Build a SkillEntry with the given metadata fields set."""
    meta = KerkovMetadata(
        category=category,
        tactic=tactic,
        mitre=mitre,
        difficulty=difficulty,
        platforms=platforms,
        tags=tags,
        products=products,
        vuln_classes=vuln_classes,
        cves=cves,
        lab_sources=lab_sources,
    )
    return SkillEntry(
        name=name,
        description=description,
        kerkov=meta,
        file_path=f"/tmp/{name}/SKILL.md",
        skill_dir=f"/tmp/{name}",
        content="body",
    )


def _names(ranked) -> list[str]:
    return [entry.name for _, entry in ranked]


class ExpandQueryTest(unittest.TestCase):

    def test_abbreviation_expands_to_phrase(self):
        tokens = expand_query("rce")
        assert {"remote", "code", "execution"} <= tokens

    def test_phrase_expands_to_abbreviation(self):
        tokens = expand_query("remote code execution")
        assert "rce" in tokens

    def test_hyphenated_alias(self):
        tokens = expand_query("priv-esc")
        assert {"privilege", "escalation"} <= tokens

    def test_unknown_token_passes_through(self):
        tokens = expand_query("nginx")
        assert tokens == {"nginx"}


class SkillRetrieverSynonymTest(unittest.TestCase):

    def setUp(self):
        self.skills = [
            _create_mock_skill(
                "web-rce", description="Achieve remote code execution on a web app"
            ),
            _create_mock_skill(
                "linux-privesc", description="Local privilege escalation on Linux"
            ),
            _create_mock_skill(
                "db-injection", description="Exploit a SQL injection to dump data"
            ),
            _create_mock_skill("unrelated", description="Enumerate DNS records"),
        ]
        self.retriever = SkillRetriever(self.skills)

    def test_rce_matches_remote_code_execution(self):
        ranked = self.retriever.rank("RCE")
        assert "web-rce" in _names(ranked)

    def test_privesc_matches_privilege_escalation(self):
        ranked = self.retriever.rank("priv-esc")
        assert "linux-privesc" in _names(ranked)

    def test_sqli_matches_sql_injection(self):
        ranked = self.retriever.rank("sqli")
        assert "db-injection" in _names(ranked)

    def test_no_match_returns_empty(self):
        ranked = self.retriever.rank("kerberoasting")
        assert ranked == []


class SkillRetrieverMetadataTest(unittest.TestCase):

    def setUp(self):
        self.skills = [
            _create_mock_skill(
                "target",
                description="A skill",
                tags=["recon"],
                category="recon/local",
                tactic="discovery",
                mitre=["T1046", "T1595"],
                products=["nginx"],
                vuln_classes=["network-service-discovery"],
                cves=["CVE-2024-9999"],
                difficulty="beginner",
                lab_sources=["vulhub"],
            ),
            _create_mock_skill("other", description="Something else entirely"),
        ]
        self.retriever = SkillRetriever(self.skills)

    def test_product_surfaces_skill(self):
        assert "target" in _names(self.retriever.rank("nginx"))

    def test_cve_surfaces_skill(self):
        assert "target" in _names(self.retriever.rank("CVE-2024-9999"))

    def test_mitre_surfaces_skill(self):
        assert "target" in _names(self.retriever.rank("T1595"))

    def test_tactic_surfaces_skill(self):
        assert "target" in _names(self.retriever.rank("discovery"))

    def test_vuln_class_surfaces_skill(self):
        assert "target" in _names(self.retriever.rank("network-service-discovery"))

    def test_lab_source_surfaces_skill(self):
        assert "target" in _names(self.retriever.rank("vulhub"))


class SkillRetrieverStopwordTest(unittest.TestCase):

    def test_alias_only_match_beats_stopword_false_positives(self):
        # "c2" expands to "command and control"; the stopword "and" must not let
        # every skill whose description contains "and" outscore the real match.
        skills = [
            _create_mock_skill(
                "c2-runbook", description="Documented procedure", tags=["c2"]
            ),
            _create_mock_skill("noise-a", description="Scan hosts and dump data"),
            _create_mock_skill("noise-b", description="Enumerate users and shares"),
        ]
        retriever = SkillRetriever(skills)
        ranked = retriever.rank("c2")
        assert _names(ranked) == ["c2-runbook"]

    def test_stopword_query_matches_nothing(self):
        skills = [_create_mock_skill("thing", description="the tool")]
        retriever = SkillRetriever(skills)
        assert retriever.rank("the") == []


class SkillRetrieverIdentifierTest(unittest.TestCase):

    def test_cve_does_not_collide_on_fragments(self):
        skills = [
            _create_mock_skill("unrelated", cves=["CVE-2024-1111", "CVE-2023-9999"]),
            _create_mock_skill("match", cves=["CVE-2024-9999"]),
        ]
        retriever = SkillRetriever(skills)
        ranked = retriever.rank("CVE-2024-9999")
        assert _names(ranked) == ["match"]

    def test_absent_cve_returns_no_match(self):
        skills = [_create_mock_skill("target", cves=["CVE-2024-9999"])]
        retriever = SkillRetriever(skills)
        assert retriever.rank("CVE-2024-0001") == []

    def test_subtechnique_is_not_matched_by_sibling(self):
        skills = [_create_mock_skill("r2-analyze", mitre=["T1059.004"])]
        retriever = SkillRetriever(skills)
        assert retriever.rank("T1059.001") == []

    def test_exact_subtechnique_matches(self):
        skills = [_create_mock_skill("r2-analyze", mitre=["T1059.004"])]
        retriever = SkillRetriever(skills)
        assert "r2-analyze" in _names(retriever.rank("T1059.004"))


class SkillRetrieverFacetTest(unittest.TestCase):

    def setUp(self):
        self.skills = [
            _create_mock_skill(
                "alpha",
                description="First",
                tags=["recon", "web"],
                category="recon/local",
                difficulty="beginner",
            ),
            _create_mock_skill(
                "beta",
                description="Second",
                tags=["exploit"],
                category="exploit/remote",
                difficulty="advanced",
            ),
        ]
        self.retriever = SkillRetriever(self.skills)

    def test_tag_filter(self):
        assert _names(self.retriever.rank(tags=["recon"])) == ["alpha"]

    def test_category_filter_substring(self):
        assert _names(self.retriever.rank(category="recon")) == ["alpha"]

    def test_difficulty_filter_exact(self):
        assert _names(self.retriever.rank(difficulty="advanced")) == ["beta"]

    def test_facet_and_query_combined(self):
        ranked = self.retriever.rank("first", tags=["recon"])
        assert _names(ranked) == ["alpha"]

    def test_facet_excludes_query_match(self):
        # "second" matches beta's description, but the recon tag filters it out.
        ranked = self.retriever.rank("second", tags=["recon"])
        assert ranked == []

    def test_no_query_no_facet_returns_empty(self):
        assert self.retriever.rank() == []


class SkillRetrieverRankingTest(unittest.TestCase):

    def test_exact_name_outranks_description_hit(self):
        skills = [
            _create_mock_skill(
                "scanner", description="Talks about the scanner tool a lot"
            ),
            _create_mock_skill("scanner-helper", description="scanner scanner scanner"),
        ]
        retriever = SkillRetriever(skills)
        ranked = retriever.rank("scanner")
        assert ranked[0][1].name == "scanner"

    def test_substring_name_outranks_metadata_only(self):
        skills = [
            _create_mock_skill("web-scanner", description="unrelated"),
            _create_mock_skill("tool", description="unrelated", tags=["scanner"]),
        ]
        retriever = SkillRetriever(skills)
        ranked = retriever.rank("scanner")
        assert ranked[0][1].name == "web-scanner"


if __name__ == "__main__":
    unittest.main()
