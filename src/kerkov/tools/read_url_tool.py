"""Fetch web pages as clean, bounded text for the search-to-read loop."""

import asyncio
from typing import Optional, Union
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import Field
from trafilatura import bare_extraction, load_html
from trafilatura.settings import Document, use_config

from kerkov.core.config_manager import ConfigManager, get_config
from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult


class ReadUrlToolParams(ToolParams):
    url: str = Field(description="HTTP or HTTPS URL of the page to read.")
    max_chars: Optional[int] = Field(
        default=None,
        description=(
            "Output character budget, including the truncation marker; defaults to "
            "and is capped by READ_MAX_CHARS."
        ),
    )
    offset: Optional[int] = Field(
        default=0,
        description=(
            "Character offset into the extracted text. Follow the truncation marker's "
            "offset to read omitted content; the tail is a preview."
        ),
    )


def _extract_content(html: Union[str, bytes], url: str) -> tuple[str, str]:
    """Extract the title and readable text locally, including short pages."""
    tree = load_html(html)
    if tree is None:
        raise ValueError("Could not parse HTML from the page")
    # Trafilatura otherwise resolves relative links against only the URL's origin.
    for base in tree.findall(".//base[@href]"):
        base.set("href", urljoin(url, base.get("href", "")))
    tree.make_links_absolute(url, resolve_base_href=True, handle_failures="ignore")
    config = use_config()
    config.set("DEFAULT", "MIN_OUTPUT_SIZE", "1")
    config.set("DEFAULT", "MIN_EXTRACTED_SIZE", "1")
    document = bare_extraction(
        tree,
        url=url,
        output_format="python",
        include_formatting=True,
        include_links=True,
        include_comments=False,
        with_metadata=True,
        config=config,
    )
    if (
        not isinstance(document, Document)
        or not document.text
        or not document.text.strip()
    ):
        raise ValueError("Could not extract readable text from the page")
    return (document.title or "").strip(), document.text.strip()


def _truncation_marker(omitted: int, total: int, next_offset: int) -> str:
    return (
        f"\n… [truncated {omitted} of {total} chars — "
        f"call again with offset={next_offset}] …\n"
    )


def _page_text(text: str, offset: int, budget: int) -> tuple[str, Optional[int]]:
    """Return a head/tail preview and the first omitted character's offset."""
    remaining = text[offset:]
    if len(remaining) <= budget:
        return remaining, None
    if budget < 2:
        raise ValueError(
            "The output budget is too small for text and a truncation marker; "
            "increase max_chars and READ_MAX_CHARS to at least 2"
        )

    # Reserve the longest possible marker so digit changes cannot exceed the cap.
    marker_size = len(_truncation_marker(len(remaining), len(text), len(text)))
    content_budget = budget - marker_size
    if content_budget < 1:
        head_length = budget - 1
        return remaining[:head_length] + "…", offset + head_length

    tail_length = content_budget // 5
    head_length = content_budget - tail_length
    next_offset = offset + head_length
    marker = _truncation_marker(len(remaining) - content_budget, len(text), next_offset)
    tail = remaining[-tail_length:] if tail_length else ""
    return remaining[:head_length] + marker + tail, next_offset


class ReadUrlTool(BaseTool):
    """Read HTML pages without adding raw markup to conversation history."""

    MAX_RESPONSE_SIZE = 10 * 1024 * 1024

    def __init__(self, *, config: Optional[ConfigManager] = None) -> None:
        super().__init__(
            name="read_url",
            description=(
                "Read an HTTP/HTTPS page as clean extracted text. Use this to "
                "deep-read URLs from web_search or CVE references. Works without "
                "a search provider or API key. Output is bounded by READ_MAX_CHARS "
                "including any truncation marker. Long pages show a head and tail "
                "preview; follow the marker's offset or metadata.next_offset to "
                "read omitted text. Each call fetches the page again. HTML only; "
                "JavaScript is not executed."
            ),
        )
        self.config = config or get_config()

    def get_params_class(self) -> type[ReadUrlToolParams]:
        return ReadUrlToolParams

    def validate_params(self, toolParams: ToolParams) -> Optional[str]:
        if not isinstance(toolParams, ReadUrlToolParams):
            return "Invalid parameters for read_url"
        url = toolParams.url.strip()
        if not url:
            return "`url` must be a non-empty string"
        try:
            authority = urlsplit(url).netloc
            parsed = httpx.URL(url)
        except (httpx.InvalidURL, ValueError):
            return "`url` must be a valid HTTP or HTTPS URL"
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.host
            or any(char.isspace() for char in authority)
        ):
            return "`url` must be an HTTP or HTTPS URL with a host"
        if toolParams.offset is not None and toolParams.offset < 0:
            return "`offset` must be greater than or equal to 0"
        if toolParams.max_chars is not None and toolParams.max_chars <= 0:
            return "`max_chars` must be greater than 0"
        return None

    async def _fetch_html(self, url: str) -> tuple[Union[str, bytes], str]:
        """Stream HTML with a guard on the decompressed response size."""
        async with httpx.AsyncClient(
            timeout=self.config.read_timeout_seconds,
            follow_redirects=False,
            headers={"Accept": "text/html,application/xhtml+xml"},
        ) as client:
            for _ in range(client.max_redirects + 1):
                async with client.stream("GET", url) as response:
                    # Automatic redirects buffer their bodies before our size guard.
                    if response.next_request is not None:
                        next_url = response.next_request.url
                        if next_url.scheme not in {"http", "https"}:
                            raise ValueError("Redirect URL must use HTTP or HTTPS")
                        url = str(next_url)
                        continue
                    return await self._read_html(response), str(response.url)
            raise httpx.TooManyRedirects("Exceeded maximum allowed redirects")

    async def _read_html(self, response: httpx.Response) -> Union[str, bytes]:
        """Read a bounded HTML response, honoring any explicit charset."""
        response.raise_for_status()
        content_type = (
            response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        )
        if content_type not in {"text/html", "application/xhtml+xml"}:
            raise ValueError(
                "Unsupported content type: "
                f"{content_type or 'missing Content-Type'}; expected HTML"
            )
        content_length = response.headers.get("content-length", "")
        if content_length.isdecimal() and int(content_length) > self.MAX_RESPONSE_SIZE:
            raise ValueError("Response exceeds the 10 MB size limit")
        content = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
            if len(content) + len(chunk) > self.MAX_RESPONSE_SIZE:
                raise ValueError("Response exceeds the 10 MB size limit")
            content.extend(chunk)
        html = bytes(content)
        if response.charset_encoding:
            try:
                return html.decode(response.charset_encoding, errors="replace")
            except LookupError:
                pass
        return html

    async def run(self, toolParams: ToolParams) -> ToolResult:
        if not isinstance(toolParams, ReadUrlToolParams):
            return ToolResult(
                success=False, output="", error="Invalid parameters for read_url"
            )
        url = toolParams.url.strip()
        offset = toolParams.offset or 0
        budget = min(
            toolParams.max_chars or self.config.read_max_chars,
            self.config.read_max_chars,
        )
        try:
            html, resolved_url = await self._fetch_html(url)
        except httpx.TimeoutException:
            return ToolResult(
                success=False,
                output="",
                error=f"Request timed out after {self.config.read_timeout_seconds} seconds",
                metadata={"url": url},
            )
        except httpx.HTTPError as exc:
            return ToolResult(
                success=False,
                output="",
                error=f"HTTP error: {exc}",
                metadata={"url": url},
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                output="",
                error=f"Page read error: {exc}",
                metadata={"url": url},
            )

        try:
            title, text = await asyncio.to_thread(_extract_content, html, resolved_url)
            output, next_offset = _page_text(text, offset, budget)
        except Exception as exc:
            return ToolResult(
                success=False,
                output="",
                error=f"Page extraction error: {exc}",
                metadata={"url": url},
            )
        return ToolResult(
            success=True,
            output=output,
            metadata={
                "url": url,
                "resolved_url": resolved_url,
                "title": title[:512],
                "total_length": len(text),
                "returned_length": len(output),
                "offset": offset,
                "truncated": next_offset is not None,
                "next_offset": next_offset,
            },
        )
