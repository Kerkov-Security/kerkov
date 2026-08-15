"""RecallTool — query the engagement knowledge base.

Stage 2 of the store-then-recall loop: the ingestion hook in
`conversation_engine` writes every tool result into the engagement's knowledge
base and replaces the large ones in history with an `artifact#N` reference.
`recall` reads them back, so the agent stops re-running enumeration it already
did and large output stops occupying the context window.

Snippet-first, like `web_search`: the default response is ranked snippets, and
`full=true` (or an `artifact#N` query) expands one record. That keeps recall
itself cheap enough to use freely.
"""

import re
from typing import Callable, List, Optional, Type

from pydantic import Field

from kerkov.core.knowledge_base.engagement_kb import EngagementKnowledgeBase
from kerkov.core.knowledge_base.models import ArtifactHit
from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult

MAX_LIMIT = 20
DEFAULT_LIMIT = 5
SNIPPET_CHARS = 400

ARTIFACT_REF_PATTERN = re.compile(r"artifact#(\d+)", re.IGNORECASE)

KnowledgeBaseGetter = Callable[[], Optional[EngagementKnowledgeBase]]


class RecallToolParams(ToolParams):
    query: str = Field(
        description=(
            "What to look for, in plain words or keywords "
            "(e.g. 'port 8080', 'SMB signing', 'creds for 10.10.10.5'). "
            "Pass 'artifact#42' to fetch that stored artifact directly."
        )
    )
    kind: Optional[str] = Field(
        default=None,
        description=(
            "Filter by artifact kind: 'tool_output', 'file', 'credential', "
            "'note', 'http_response'."
        ),
    )
    source: Optional[str] = Field(
        default=None,
        description=(
            "Filter by source substring — the tool name, host or URL that "
            "produced the artifact (e.g. 'exec', 'http', '10.10.10.5')."
        ),
    )
    engagement: Optional[str] = Field(
        default=None,
        description=(
            "Restrict to one engagement id. Omit to search this engagement and "
            "every agent spawned from it. Pass the id printed next to a "
            "delegated agent's report to read the raw output behind its "
            "summarized findings."
        ),
    )
    limit: Optional[int] = Field(
        default=DEFAULT_LIMIT,
        description=f"Maximum results to return (capped at {MAX_LIMIT}).",
    )
    full: Optional[bool] = Field(
        default=False,
        description=(
            "Return each match's complete stored body instead of a snippet. "
            "Use after a snippet shows the artifact is the one you need."
        ),
    )


class RecallTool(BaseTool):
    """Search everything this engagement (and its agents) has gathered."""

    def __init__(self, kb_getter: KnowledgeBaseGetter):
        super().__init__(
            name="recall",
            description=(
                "Search the engagement knowledge base — the full output of every "
                "tool run so far in this engagement, including runs by delegated "
                "agents. Use it before re-running a scan or re-fetching a page: "
                "if you already did it, the output is here in full even when the "
                "conversation only kept a short reference to it. Returns ranked "
                "snippets by default; set full=true or query 'artifact#N' to "
                "expand one record. When a delegated agent's findings are too "
                "thin, pass its engagement id as `engagement` to read the raw "
                "output behind them."
            ),
        )
        # Late-bound: the engagement id does not exist when tools are built.
        # It is minted on the first message of a conversation (or restored on
        # resume), so the knowledge base has to be fetched per call.
        self._kb_getter = kb_getter

    def get_params_class(self) -> Type[ToolParams]:
        return RecallToolParams

    def validate_params(self, toolParams: ToolParams) -> Optional[str]:
        assert isinstance(toolParams, RecallToolParams)
        if not toolParams.query or not toolParams.query.strip():
            return "query is required."
        if toolParams.limit is not None and toolParams.limit < 1:
            return f"limit must be at least 1, got {toolParams.limit}."
        return None

    async def run(self, toolParams: ToolParams) -> ToolResult:
        assert isinstance(toolParams, RecallToolParams)
        kb = self._kb_getter()
        if kb is None:
            return ToolResult(
                success=False,
                output="",
                error=(
                    "No knowledge base is active for this session, so there is "
                    "nothing to recall yet."
                ),
            )

        limit = min(toolParams.limit or DEFAULT_LIMIT, MAX_LIMIT)

        direct = ARTIFACT_REF_PATTERN.search(toolParams.query)
        if direct:
            return await self._expand_artifact(kb, int(direct.group(1)))

        hits = await kb.search(
            toolParams.query,
            engagement_id=toolParams.engagement,
            kind=toolParams.kind,
            source=toolParams.source,
            limit=limit,
            full=bool(toolParams.full),
        )

        if not hits:
            return await self._empty_result(kb, toolParams)

        return ToolResult(
            success=True,
            output=self._format_hits(hits, full=bool(toolParams.full)),
            metadata={
                "count": len(hits),
                "hits": [
                    {
                        "artifact_id": h.artifact_id,
                        "engagement_id": h.engagement_id,
                        "kind": h.kind,
                        "source": h.source,
                        "title": h.title,
                        "size_bytes": h.size_bytes,
                        "agent_type": h.agent_type,
                        **h.metadata,
                    }
                    for h in hits
                ],
            },
        )

    async def _expand_artifact(
        self, kb: EngagementKnowledgeBase, artifact_id: int
    ) -> ToolResult:
        hit = await kb.get_artifact(artifact_id, full=True)
        if hit is None:
            return ToolResult(
                success=False,
                output="",
                error=(
                    f"No artifact#{artifact_id} in this engagement's knowledge "
                    f"base."
                ),
            )
        header = self._format_header(hit)
        return ToolResult(
            success=True,
            output=f"{header}\n{hit.body or ''}",
            metadata={
                "artifact_id": hit.artifact_id,
                "engagement_id": hit.engagement_id,
                "kind": hit.kind,
                "source": hit.source,
                "size_bytes": hit.size_bytes,
                **hit.metadata,
            },
        )

    async def _empty_result(
        self, kb: EngagementKnowledgeBase, toolParams: RecallToolParams
    ) -> ToolResult:
        """Explain *why* nothing came back.

        "That agent recorded nothing" and "it recorded plenty, none matching"
        are different facts, and the second is the one that should send the
        model back with a different query rather than off to re-run the work.
        """
        scope = toolParams.engagement
        stored = await kb.count(scope)
        if scope and stored == 0:
            known = await kb.engagement_ids()
            suffix = (
                f" Engagements with artifacts: {', '.join(known)}." if known else ""
            )
            return ToolResult(
                success=True,
                output=(
                    f"No artifacts recorded for engagement '{scope}'. It may "
                    f"still be running, or it produced no tool output.{suffix}"
                ),
                metadata={"count": 0, "stored_in_scope": 0, "engagement": scope},
            )

        where = f" for engagement '{scope}'" if scope else ""
        return ToolResult(
            success=True,
            output=(
                f"No stored artifact matches '{toolParams.query}'{where} "
                f"({stored} artifact(s) searched). Try broader keywords, or drop "
                f"the kind/source filters."
            ),
            metadata={"count": 0, "stored_in_scope": stored},
        )

    def _format_hits(self, hits: List[ArtifactHit], *, full: bool) -> str:
        blocks: List[str] = []
        for hit in hits:
            header = self._format_header(hit)
            if full and hit.body is not None:
                blocks.append(f"{header}\n{hit.body}")
            else:
                snippet = hit.snippet[:SNIPPET_CHARS]
                blocks.append(f"{header}\n{snippet}")
        footer = (
            ""
            if full
            else "\n\nExpand any of these with recall(query='artifact#<id>')."
        )
        return "\n\n".join(blocks) + footer

    @staticmethod
    def _format_header(hit: ArtifactHit) -> str:
        parts = [f"artifact#{hit.artifact_id}"]
        if hit.title:
            parts.append(hit.title)
        if hit.source:
            parts.append(f"via {hit.source}")
        origin = hit.agent_type or "this session"
        parts.append(f"[{hit.kind}, {hit.size_bytes} B, {origin}: {hit.engagement_id}]")
        return "— " + " | ".join(parts)
