"""Per-engagement knowledge base: SQLite + FTS5 over everything the agent gathered.

One database per *root* engagement, not per engagement id. A spawned agent gets
its own engagement id (`EngagementStore.start_engagement(kind="agent", ...)`),
so keying the file on that id would give every agent a private database the
master could never read — its enumeration would be indexed into the wrong file.
Instead `engagement_id` is a column, the file is named for the root, and recall
searches the whole tree by default with an optional per-engagement filter. That
is what lets the master drill into the raw output behind an agent's summarized
FINDINGS, using the engagement id already printed alongside them.

Keyword search is FTS5 from the stdlib `sqlite3` — no new dependency, fully
offline. Semantic recall is opt-in behind `EmbeddingBackend` and blends with
the keyword hits when enabled.
"""

import asyncio
import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

from kerkov.core.knowledge_base.embeddings import (
    EmbeddingBackend,
    chunk_text,
    cosine_similarity,
    pack_vector,
    unpack_vector,
)
from kerkov.core.knowledge_base.models import Artifact, ArtifactHit, ArtifactKind
from kerkov.core.logging.logger_factory import get_logger

logger = get_logger("EngagementKB")

SCHEMA_VERSION = 1

# Keeps IPs, ports, paths, hashes and CVE ids intact as phrase queries; drops
# the punctuation that would otherwise be read as FTS5 operator syntax.
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_.:/@-]+")

# A spawn chain is bounded by AGENT_MAX_DEPTH, but the walk is defensive: a
# hand-edited or corrupted metadata header must not hang the CLI.
MAX_PARENT_HOPS = 16

MAX_SEMANTIC_CANDIDATES = 500


def kb_path_for(store_dir: Path, root_engagement_id: str) -> Path:
    """Path of the knowledge base file for a root engagement."""
    return store_dir / f"{root_engagement_id}.kb.sqlite"


def resolve_root_engagement_id(
    engagement_id: str,
    parent_of: Callable[[str], Optional[str]],
    max_hops: int = MAX_PARENT_HOPS,
) -> str:
    """Walk `parent_engagement_id` up to the root of the engagement tree.

    Today's spawn wiring records the master as the parent of *every* agent
    regardless of depth (`SpawnAgentTool` hands nested spawn tools the master's
    store), so this resolves in one hop. It is written as a bounded loop so a
    later fix to that parentage — making the tree genuinely multi-level — does
    not silently start keying depth-2 agents to their depth-1 parent's file.
    """
    seen: Set[str] = {engagement_id}
    current = engagement_id
    for _ in range(max_hops):
        parent = parent_of(current)
        if not parent or parent in seen:
            return current
        seen.add(parent)
        current = parent
    logger.warning(
        f"Parent chain for engagement {engagement_id} exceeded {max_hops} hops; "
        f"treating {current} as root"
    )
    return current


def build_match_query(raw: str, operator: str = "AND") -> Optional[str]:
    """Turn a free-text query into a safe FTS5 MATCH expression.

    The query comes from the model, so it arrives as natural language with
    apostrophes, question marks and slashes that FTS5 would either reject as
    malformed syntax or silently interpret as operators. Each token is quoted
    as a phrase instead, which both neutralizes the syntax and keeps dotted
    forms like `10.0.0.5` matching as a unit under the default tokenizer.
    """
    tokens = [t for t in _TOKEN_PATTERN.findall(raw) if t.strip("._:/@-")]
    if not tokens:
        return None
    quoted = ['"' + t.replace('"', '""') + '"' for t in tokens]
    return f" {operator} ".join(quoted)


class EngagementKnowledgeBase:
    """SQLite/FTS5 store of artifacts for one engagement tree.

    The connection is opened lazily on first use and guarded by a lock: a
    delegated batch runs its agents through `asyncio.gather`, so several
    sibling agents write to this one file concurrently.
    """

    def __init__(
        self,
        root_engagement_id: str,
        store_dir: Path,
        embedding_backend: Optional[EmbeddingBackend] = None,
    ) -> None:
        self.root_engagement_id = root_engagement_id
        self.store_dir = Path(store_dir)
        self.path = kb_path_for(self.store_dir, root_engagement_id)
        self._embedding_backend = embedding_backend
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Connection / schema
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        self.store_dir.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False because the web console runs the loop under
        # Channels' thread pool; every access is serialized by self._lock.
        conn = sqlite3.connect(str(self.path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # WAL lets a reader (one agent's recall) proceed while a sibling
        # writes; busy_timeout absorbs the remaining write contention.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        self._migrate(conn)
        self._conn = conn
        return conn

    def _migrate(self, conn: sqlite3.Connection) -> None:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version >= SCHEMA_VERSION:
            return
        with conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS artifacts (
                    id            INTEGER PRIMARY KEY,
                    engagement_id TEXT NOT NULL,
                    kind          TEXT NOT NULL,
                    source        TEXT NOT NULL DEFAULT '',
                    title         TEXT NOT NULL DEFAULT '',
                    body          TEXT NOT NULL DEFAULT '',
                    metadata      TEXT NOT NULL DEFAULT '{}',
                    agent_type    TEXT,
                    size_bytes    INTEGER NOT NULL DEFAULT 0,
                    created_at    TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_artifacts_engagement
                    ON artifacts(engagement_id);
                CREATE INDEX IF NOT EXISTS idx_artifacts_kind
                    ON artifacts(kind);

                CREATE VIRTUAL TABLE IF NOT EXISTS artifacts_fts USING fts5(
                    title, source, body,
                    content='artifacts',
                    content_rowid='id'
                );

                CREATE TRIGGER IF NOT EXISTS artifacts_ai AFTER INSERT ON artifacts
                BEGIN
                    INSERT INTO artifacts_fts(rowid, title, source, body)
                    VALUES (new.id, new.title, new.source, new.body);
                END;

                CREATE TRIGGER IF NOT EXISTS artifacts_ad AFTER DELETE ON artifacts
                BEGIN
                    INSERT INTO artifacts_fts(artifacts_fts, rowid, title, source, body)
                    VALUES ('delete', old.id, old.title, old.source, old.body);
                END;

                CREATE TABLE IF NOT EXISTS artifact_embeddings (
                    artifact_id INTEGER NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    chunk_text  TEXT NOT NULL,
                    vector      BLOB NOT NULL,
                    PRIMARY KEY (artifact_id, chunk_index),
                    FOREIGN KEY (artifact_id) REFERENCES artifacts(id) ON DELETE CASCADE
                );
                """)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    async def add_artifact(
        self,
        *,
        engagement_id: str,
        body: str,
        kind: ArtifactKind = ArtifactKind.TOOL_OUTPUT,
        source: str = "",
        title: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        agent_type: Optional[str] = None,
    ) -> int:
        """Store one artifact, returning its id.

        Returns the artifact id so the caller can put `artifact#N` into the
        conversation as the handle for a later `recall` expansion.
        """
        artifact = Artifact(
            engagement_id=engagement_id,
            kind=kind,
            source=source,
            title=title,
            body=body,
            metadata=metadata or {},
            agent_type=agent_type,
        )
        created_at = datetime.now(timezone.utc).isoformat()
        size_bytes = len(body.encode("utf-8"))

        def _insert() -> int:
            with self._lock:
                conn = self._connect()
                with conn:
                    cursor = conn.execute(
                        """
                        INSERT INTO artifacts
                            (engagement_id, kind, source, title, body,
                             metadata, agent_type, size_bytes, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            artifact.engagement_id,
                            artifact.kind.value,
                            artifact.source,
                            artifact.title,
                            artifact.body,
                            json.dumps(artifact.metadata, default=str),
                            artifact.agent_type,
                            size_bytes,
                            created_at,
                        ),
                    )
                return int(cursor.lastrowid or 0)

        artifact_id = await asyncio.to_thread(_insert)
        if self._embedding_backend is not None:
            await self._embed_artifact(artifact_id, body)
        return artifact_id

    async def _embed_artifact(self, artifact_id: int, body: str) -> None:
        """Chunk, embed and store vectors for one artifact."""
        backend = self._embedding_backend
        if backend is None:
            return
        chunks = chunk_text(body)
        if not chunks:
            return
        try:
            vectors = await backend.embed(chunks)
        except Exception as e:  # a backend outage must not lose the artifact
            logger.warning(f"Embedding failed for artifact#{artifact_id}: {e}")
            return

        rows = [
            (artifact_id, i, chunk, pack_vector(vector))
            for i, (chunk, vector) in enumerate(zip(chunks, vectors))
        ]

        def _insert() -> None:
            with self._lock:
                conn = self._connect()
                with conn:
                    conn.executemany(
                        """
                        INSERT OR REPLACE INTO artifact_embeddings
                            (artifact_id, chunk_index, chunk_text, vector)
                        VALUES (?, ?, ?, ?)
                        """,
                        rows,
                    )

        await asyncio.to_thread(_insert)

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        *,
        engagement_id: Optional[str] = None,
        kind: Optional[ArtifactKind] = None,
        source: Optional[str] = None,
        limit: int = 5,
        full: bool = False,
    ) -> List[ArtifactHit]:
        """Ranked artifacts matching `query`.

        Searches the whole engagement tree unless `engagement_id` narrows it to
        one run — the filter the master uses to drill into a named agent.
        """
        hits = await asyncio.to_thread(
            self._search_keyword, query, engagement_id, kind, source, limit, full
        )
        if self._embedding_backend is None:
            return hits
        return await self._blend_semantic(
            query, hits, engagement_id, kind, source, limit, full
        )

    def _search_keyword(
        self,
        query: str,
        engagement_id: Optional[str],
        kind: Optional[ArtifactKind],
        source: Optional[str],
        limit: int,
        full: bool,
    ) -> List[ArtifactHit]:
        match = build_match_query(query, "AND")
        if match is None:
            return []
        rows = self._run_match(match, engagement_id, kind, source, limit)
        if not rows:
            # Every term ANDed is precise but brittle for a natural-language
            # question; fall back to any-term before reporting nothing.
            or_match = build_match_query(query, "OR")
            if or_match and or_match != match:
                rows = self._run_match(or_match, engagement_id, kind, source, limit)
        return [self._row_to_hit(row, full=full) for row in rows]

    def _run_match(
        self,
        match: str,
        engagement_id: Optional[str],
        kind: Optional[ArtifactKind],
        source: Optional[str],
        limit: int,
    ) -> List[sqlite3.Row]:
        sql = [
            "SELECT a.*, snippet(artifacts_fts, 2, '', '', ' … ', 24) AS snip",
            "FROM artifacts_fts",
            "JOIN artifacts a ON a.id = artifacts_fts.rowid",
            "WHERE artifacts_fts MATCH ?",
        ]
        params: List[Any] = [match]
        if engagement_id:
            sql.append("AND a.engagement_id = ?")
            params.append(engagement_id)
        if kind:
            sql.append("AND a.kind = ?")
            params.append(kind.value)
        if source:
            sql.append("AND a.source LIKE ?")
            params.append(f"%{source}%")
        sql.append("ORDER BY rank LIMIT ?")
        params.append(limit)

        with self._lock:
            conn = self._connect()
            try:
                return list(conn.execute(" ".join(sql), params).fetchall())
            except sqlite3.OperationalError as e:
                logger.warning(f"FTS query failed ({match!r}): {e}")
                return []

    async def _blend_semantic(
        self,
        query: str,
        keyword_hits: List[ArtifactHit],
        engagement_id: Optional[str],
        kind: Optional[ArtifactKind],
        source: Optional[str],
        limit: int,
        full: bool,
    ) -> List[ArtifactHit]:
        """Append semantic matches the keyword pass missed, keyword-first."""
        backend = self._embedding_backend
        if backend is None:
            return keyword_hits
        try:
            query_vectors = await backend.embed([query])
        except Exception as e:
            logger.warning(f"Embedding the recall query failed: {e}")
            return keyword_hits
        if not query_vectors:
            return keyword_hits
        query_vector = query_vectors[0]

        def _scan() -> List[tuple]:
            with self._lock:
                conn = self._connect()
                sql = [
                    "SELECT e.artifact_id, e.chunk_text, e.vector",
                    "FROM artifact_embeddings e",
                    "JOIN artifacts a ON a.id = e.artifact_id",
                    "WHERE 1=1",
                ]
                params: List[Any] = []
                if engagement_id:
                    sql.append("AND a.engagement_id = ?")
                    params.append(engagement_id)
                if kind:
                    sql.append("AND a.kind = ?")
                    params.append(kind.value)
                if source:
                    sql.append("AND a.source LIKE ?")
                    params.append(f"%{source}%")
                sql.append("LIMIT ?")
                params.append(MAX_SEMANTIC_CANDIDATES)
                rows = conn.execute(" ".join(sql), params).fetchall()
            scored = [
                (
                    cosine_similarity(query_vector, unpack_vector(row["vector"])),
                    row["artifact_id"],
                    row["chunk_text"],
                )
                for row in rows
            ]
            scored.sort(key=lambda s: s[0], reverse=True)
            return scored

        scored = await asyncio.to_thread(_scan)

        seen = {hit.artifact_id for hit in keyword_hits}
        blended = list(keyword_hits)
        for score, artifact_id, chunk in scored:
            if len(blended) >= limit:
                break
            if artifact_id in seen or score <= 0.0:
                continue
            hit = await self.get_artifact(artifact_id, full=full)
            if hit is None:
                continue
            hit.snippet = chunk.strip()
            hit.metadata = {**hit.metadata, "semantic_score": round(score, 4)}
            blended.append(hit)
            seen.add(artifact_id)
        return blended

    async def get_artifact(
        self, artifact_id: int, *, full: bool = True
    ) -> Optional[ArtifactHit]:
        """Fetch one artifact by id — the `artifact#N` expansion path."""

        def _fetch() -> Optional[sqlite3.Row]:
            with self._lock:
                conn = self._connect()
                row: Optional[sqlite3.Row] = conn.execute(
                    "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
                ).fetchone()
            return row

        row = await asyncio.to_thread(_fetch)
        if row is None:
            return None
        return self._row_to_hit(row, full=full)

    async def count(self, engagement_id: Optional[str] = None) -> int:
        """How many artifacts are stored, optionally for one engagement.

        Recall uses this to tell "that engagement recorded nothing" apart from
        "it recorded plenty, none of it matching your query".
        """

        def _count() -> int:
            with self._lock:
                conn = self._connect()
                if engagement_id:
                    row = conn.execute(
                        "SELECT COUNT(*) FROM artifacts WHERE engagement_id = ?",
                        (engagement_id,),
                    ).fetchone()
                else:
                    row = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()
            return int(row[0])

        return await asyncio.to_thread(_count)

    async def engagement_ids(self) -> List[str]:
        """Engagement ids that have contributed artifacts, most recent first."""

        def _ids() -> List[str]:
            with self._lock:
                conn = self._connect()
                rows = conn.execute("""
                    SELECT engagement_id, MAX(created_at) AS last_at
                    FROM artifacts GROUP BY engagement_id
                    ORDER BY last_at DESC
                    """).fetchall()
            return [row["engagement_id"] for row in rows]

        return await asyncio.to_thread(_ids)

    def delete_engagement_rows(self, engagement_id: str) -> int:
        """Drop one engagement's artifacts, leaving the rest of the tree intact.

        Deleting an agent sub-run must not take the shared database with it.
        """
        with self._lock:
            conn = self._connect()
            with conn:
                conn.execute(
                    """
                    DELETE FROM artifact_embeddings WHERE artifact_id IN
                        (SELECT id FROM artifacts WHERE engagement_id = ?)
                    """,
                    (engagement_id,),
                )
                cursor = conn.execute(
                    "DELETE FROM artifacts WHERE engagement_id = ?", (engagement_id,)
                )
            return int(cursor.rowcount or 0)

    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_hit(row: sqlite3.Row, *, full: bool) -> ArtifactHit:
        keys = row.keys()
        try:
            metadata = json.loads(row["metadata"]) if row["metadata"] else {}
        except json.JSONDecodeError:
            metadata = {}
        snippet = row["snip"] if "snip" in keys else ""
        if not snippet:
            snippet = row["body"][:240]
        return ArtifactHit(
            artifact_id=int(row["id"]),
            engagement_id=row["engagement_id"],
            kind=row["kind"],
            source=row["source"],
            title=row["title"],
            metadata=metadata if isinstance(metadata, dict) else {},
            agent_type=row["agent_type"],
            created_at=row["created_at"],
            size_bytes=int(row["size_bytes"]),
            snippet=snippet.strip(),
            body=row["body"] if full else None,
        )


def delete_kb_for_root(store_dir: Path, root_engagement_id: str) -> bool:
    """Delete the knowledge base file for a root engagement, if present."""
    path = kb_path_for(Path(store_dir), root_engagement_id)
    deleted = False
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if candidate.exists():
            candidate.unlink()
            deleted = deleted or candidate == path
    return deleted


def open_knowledge_base(
    root_engagement_id: str,
    store_dir: Path,
    *,
    embedding_backend: Optional[EmbeddingBackend] = None,
) -> EngagementKnowledgeBase:
    """Construct a knowledge base for an already-resolved root engagement."""
    return EngagementKnowledgeBase(
        root_engagement_id=root_engagement_id,
        store_dir=store_dir,
        embedding_backend=embedding_backend,
    )


__all__: Sequence[str] = (
    "EngagementKnowledgeBase",
    "build_match_query",
    "delete_kb_for_root",
    "kb_path_for",
    "open_knowledge_base",
    "resolve_root_engagement_id",
)
