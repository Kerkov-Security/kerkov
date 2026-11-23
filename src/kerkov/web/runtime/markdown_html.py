"""Render assistant Markdown as HTML that is safe to place in the console."""

from __future__ import annotations

from typing import Sequence

from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml
from markdown_it.renderer import RendererHTML
from markdown_it.token import Token
from markdown_it.utils import EnvType, OptionsDict

_LINK_ATTRS = 'target="_blank" rel="noopener noreferrer"'


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


def _inside_link(tokens: Sequence[Token], idx: int) -> bool:
    depth = 0
    for token in tokens[:idx]:
        depth += {"link_open": 1, "link_close": -1}.get(token.type, 0)
    return depth > 0


def _plain_text(tokens: Sequence[Token]) -> str:
    """Flatten inline tokens to text, keeping code spans that alt text would drop."""
    text = ""
    for token in tokens:
        if token.type in ("text", "code_inline"):
            text += token.content
        elif token.type == "image":
            text += _plain_text(token.children or [])
        elif token.type in ("softbreak", "hardbreak"):
            text += "\n"
    return text


def _image_as_link(
    renderer: RendererHTML,
    tokens: Sequence[Token],
    idx: int,
    options: OptionsDict,
    env: EnvType,
) -> str:
    """Render an image as a link to it, so nothing is fetched until clicked."""
    token = tokens[idx]
    src = str(token.attrGet("src") or "")
    alt = _plain_text(token.children or [])
    label = escapeHtml(alt or src)
    if _inside_link(tokens, idx):
        return label
    return f'<a href="{escapeHtml(src)}" {_LINK_ATTRS}>{label}</a>'


def _build_renderer() -> MarkdownIt:
    # Raw HTML is escaped and the default link validator rejects script-capable
    # schemes, for image sources too. Images never render as <img>: the browser
    # would fetch them unprompted, letting injected text exfiltrate data
    # through a URL.
    md = MarkdownIt("commonmark", {"html": False, "breaks": True}).enable(
        ["table", "strikethrough"]
    )
    md.add_render_rule("link_open", _external_link_open)
    md.add_render_rule("image", _image_as_link)
    return md


_RENDERER = _build_renderer()


def render_markdown(text: str) -> str:
    """Return inert HTML for model-written Markdown."""
    html: str = _RENDERER.render(text)
    return html
