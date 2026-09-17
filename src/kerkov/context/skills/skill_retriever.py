"""
SkillRetriever — offline hybrid keyword ranking over parsed skills.

Ranks the loaded ``SkillEntry`` set with token-overlap scoring, a curated
synonym/alias map (so ``rce`` matches "remote code execution"), and faceted
``tags``/``category``/``difficulty`` filters. No dependency, no network.
"""

import re
from typing import Optional

from kerkov.context.skills.models import SkillEntry

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Dropped from every tokenized set: they carry no retrieval signal but appear in
# spelled-out synonym phrases ("command and control", "denial of service"), so
# without this a stopword expanded from one alias matches every prose field.
_STOPWORDS = frozenset(
    {"a", "an", "and", "as", "at", "by", "for", "in", "of", "on", "or", "the", "to"}
)

_IDENT_STRIP = ".,;:()[]{}'\"`"


# Curated equivalence groups: every phrase in a group expands to every other.
# Applied to the query before scoring so abbreviations match spelled-out
# descriptions and vice versa. Plain data — extend freely.
SYNONYM_GROUPS: list[list[str]] = [
    ["rce", "remote code execution"],
    ["lpe", "privesc", "priv-esc", "privilege escalation"],
    ["sqli", "sql injection"],
    ["xss", "cross site scripting", "cross-site scripting"],
    ["csrf", "cross site request forgery", "cross-site request forgery"],
    ["ssrf", "server side request forgery", "server-side request forgery"],
    ["lfi", "local file inclusion"],
    ["rfi", "remote file inclusion"],
    ["xxe", "xml external entity"],
    ["idor", "insecure direct object reference"],
    ["ssti", "server side template injection", "server-side template injection"],
    ["dos", "denial of service"],
    ["mitm", "man in the middle", "man-in-the-middle"],
    ["c2", "command and control"],
    ["recon", "reconnaissance"],
    ["enum", "enumeration"],
    ["creds", "credentials"],
    ["cred", "credential"],
    ["auth", "authentication"],
    ["authz", "authorization"],
    ["dirbust", "directory brute force", "directory bruteforce"],
    ["poc", "proof of concept"],
    ["deserialization", "deserialisation", "insecure deserialization"],
]


def _tokenize(text: str) -> list[str]:
    """Lowercase, split into alphanumeric tokens, and drop stopwords."""
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


def _identifier_tokens(text: str) -> set[str]:
    """Whole normalized identifiers from text (whitespace-split, punctuation-trimmed)."""
    out: set[str] = set()
    for chunk in text.lower().split():
        chunk = chunk.strip(_IDENT_STRIP)
        if chunk:
            out.add(chunk)
    return out


def _technique_tokens(text: str) -> set[str]:
    """Identifiers from text, plus the parent technique of each ATT&CK sub-technique."""
    out = _identifier_tokens(text)
    for identifier in tuple(out):
        parent, _, sub = identifier.partition(".")
        if parent and sub:
            out.add(parent)
    return out


# cves/mitre hold structured identifiers (CVE-2024-9999, T1059.004); prose
# tokenization would split them into fragments that collide across records, so
# these fields index and match on the whole normalized identifier instead. mitre
# also indexes the parent of each sub-technique, so a query for T1059 still
# reaches T1059.004 without T1059.001 matching it.
_FIELD_SPLITTERS = {
    "cves": _identifier_tokens,
    "mitre": _technique_tokens,
}
_IDENTIFIER_FIELDS = frozenset(_FIELD_SPLITTERS)


def _build_synonym_index() -> dict[str, list[list[str]]]:
    """Map each token of each phrase to the groups that contain it."""
    index: dict[str, list[list[str]]] = {}
    for group in SYNONYM_GROUPS:
        for phrase in group:
            for token in _tokenize(phrase):
                groups = index.setdefault(token, [])
                if group not in groups:
                    groups.append(group)
    return index


_SYNONYM_INDEX = _build_synonym_index()


def _group_triggered(group: list[str], query_tokens: set[str], lowered: str) -> bool:
    """Whether any phrase in a group is present in the query."""
    for phrase in group:
        phrase_tokens = _tokenize(phrase)
        if len(phrase_tokens) == 1:
            if phrase_tokens[0] in query_tokens:
                return True
        elif phrase in lowered:
            return True
    return False


def expand_query(query: str) -> set[str]:
    """Return the query's tokens plus every synonym-group expansion it triggers."""
    lowered = query.lower()
    tokens = set(_tokenize(query))

    candidates: list[list[str]] = []
    for token in tokens:
        for group in _SYNONYM_INDEX.get(token, ()):
            if group not in candidates:
                candidates.append(group)

    for group in candidates:
        if _group_triggered(group, tokens, lowered):
            for phrase in group:
                tokens.update(_tokenize(phrase))
    return tokens


# Field weights: name hits dominate, description next, metadata last. Exact and
# substring name matches are scored separately so an exact name always wins.
_FIELD_WEIGHTS = {
    "name": 8,
    "description": 5,
    "tags": 3,
    "category": 3,
    "tactic": 3,
    "products": 2,
    "vuln_classes": 2,
    "mitre": 2,
    "cves": 2,
    "platforms": 2,
    "lab_sources": 2,
}

_EXACT_NAME_BONUS = 100
_SUBSTRING_NAME_BONUS = 50


class SkillRetriever:
    """Ranks skills by keyword overlap with synonym expansion and facet filters."""

    def __init__(self, skills: list[SkillEntry]):
        self._skills = skills
        self._field_tokens: dict[str, dict[str, set[str]]] = {}
        for entry in skills:
            self._field_tokens[entry.name] = self._index_entry(entry)

    @staticmethod
    def _index_entry(entry: SkillEntry) -> dict[str, set[str]]:
        """Tokenize each searchable field of a skill once, up front."""
        fields: dict[str, set[str]] = {
            "name": set(_tokenize(entry.name)),
            "description": set(_tokenize(entry.description)),
        }
        meta = entry.kerkov
        for field_name in (
            "tags",
            "category",
            "tactic",
            "products",
            "vuln_classes",
            "mitre",
            "cves",
            "platforms",
            "lab_sources",
        ):
            split = _FIELD_SPLITTERS.get(field_name, _tokenize)
            tokens: set[str] = set()
            if meta:
                value = getattr(meta, field_name, None)
                if isinstance(value, str):
                    tokens = set(split(value))
                elif value:
                    for item in value:
                        tokens.update(split(item))
            fields[field_name] = tokens
        return fields

    def rank(
        self,
        query: Optional[str] = None,
        *,
        tags: Optional[list[str]] = None,
        category: Optional[str] = None,
        difficulty: Optional[str] = None,
    ) -> list[tuple[float, SkillEntry]]:
        """Rank skills that pass the facet filters, highest score first.

        Returns empty when no query and no filters are given.
        """
        if not any([query, tags, category, difficulty]):
            return []

        query_tokens = expand_query(query) if query else set()
        query_identifiers = _identifier_tokens(query) if query else set()
        query_lower = query.lower().strip() if query else None

        results: list[tuple[float, SkillEntry]] = []
        for entry in self._skills:
            if not self._passes_facets(entry, tags, category, difficulty):
                continue

            if query_lower is None:
                results.append((0.0, entry))
                continue

            score = self._keyword_score(
                entry, query_tokens, query_identifiers, query_lower
            )
            if score > 0:
                results.append((score, entry))

        results.sort(key=lambda item: item[0], reverse=True)
        return results

    def _keyword_score(
        self,
        entry: SkillEntry,
        query_tokens: set[str],
        query_identifiers: set[str],
        query_lower: str,
    ) -> float:
        """Score one skill against expanded query tokens."""
        fields = self._field_tokens[entry.name]
        name_lower = entry.name.lower()

        score = 0.0
        if name_lower == query_lower:
            score += _EXACT_NAME_BONUS
        elif query_lower in name_lower:
            score += _SUBSTRING_NAME_BONUS

        for field_name, weight in _FIELD_WEIGHTS.items():
            probe = (
                query_identifiers if field_name in _IDENTIFIER_FIELDS else query_tokens
            )
            matched = probe & fields[field_name]
            score += weight * len(matched)
        return score

    @staticmethod
    def _passes_facets(
        entry: SkillEntry,
        tags: Optional[list[str]],
        category: Optional[str],
        difficulty: Optional[str],
    ) -> bool:
        """Hard AND filter over tags, category, and difficulty."""
        meta = entry.kerkov

        if tags:
            entry_tags = {t.lower() for t in (meta.tags or [])} if meta else set()
            if not all(t.lower() in entry_tags for t in tags):
                return False

        if category:
            entry_category = meta.category.lower() if meta and meta.category else ""
            if category.lower() not in entry_category:
                return False

        if difficulty:
            entry_difficulty = (
                meta.difficulty.lower() if meta and meta.difficulty else ""
            )
            if difficulty.lower() != entry_difficulty:
                return False

        return True
