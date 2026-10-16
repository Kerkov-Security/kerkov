"""Render assistant Markdown as HTML that is safe to place in the console."""

from __future__ import annotations

from typing import Sequence

from markdown_it import MarkdownIt
from markdown_it.renderer import RendererHTML
from markdown_it.token import Token
from markdown_it.utils import EnvType, OptionsDict


def _external_link_open(
    renderer: RendererHTML,
    tokens: Sequence[Token],
    idx: int,
    options: OptionsDict,
    env: EnvType,
) -> str:
    tokens[idx].attrSet("target", "_blank")
    tokens[idx].attrSet("rel", "noopener noreferrer")
    return renderer.renderToken(tokens, idx, options, env)


def _build_renderer() -> MarkdownIt:
    # Raw HTML is escaped and the default link validator rejects script-capable
    # schemes. Images are disabled because the browser would fetch them
    # unprompted, letting injected text exfiltrate data through a URL.
    md = (
        MarkdownIt("commonmark", {"html": False, "breaks": True})
        .enable(["table", "strikethrough"])
        .disable("image")
    )
    md.add_render_rule("link_open", _external_link_open)
    return md


_RENDERER = _build_renderer()


def render_markdown(text: str) -> str:
    """Return inert HTML for model-written Markdown."""
    html: str = _RENDERER.render(text)
    return html
