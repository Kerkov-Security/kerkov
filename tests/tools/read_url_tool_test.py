"""Offline coverage of page extraction, bounded output, and continuation reads."""

import asyncio
import gzip
import re
import unittest
from typing import Any, AsyncIterator, Callable, Dict, List, Optional
from unittest.mock import patch

import httpx
from trafilatura.settings import Document

from kerkov.core.config_manager import ConfigManager
from kerkov.tools.base_tool import ToolResult
from kerkov.tools.read_url_tool import ReadUrlTool

_REAL_ASYNC_CLIENT = httpx.AsyncClient
_URL = "https://example.com/release"
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
_MARKER = re.compile(
    r"\n… \[truncated (\d+) of (\d+) chars — call again with offset=(\d+)\] …\n"
)
_ARTICLE = """<!doctype html>
<html><head><title>Security release notes</title>
<style>.secret-style { color: red; }</style>
<script>const secretScript = 'ignore';</script></head><body>
<header>Site header boilerplate</header>
<nav><a href="/home">Navigation only</a><a href="/login">Sign in</a></nav>
<main><article><h1>Security release notes</h1>
<p>The release fixes a request parsing vulnerability in the server.
Administrators should review the documented configuration before deploying
the update.</p><h2>Upgrade guidance</h2>
<p>Apply the supported update and restart the service after checking the new
behavior. Verify that normal requests still succeed after deployment.</p>
<ul><li>Back up the service configuration before starting the upgrade.</li>
<li>Install the patched version from the official release channel.</li></ul>
<pre><code>server --version
server --check-config</code></pre>
<p>This release also improves diagnostic messages for malformed requests.
Consult the release notes for the complete list of supported platforms and
operational changes.</p></article></main>
<aside class="advertisement">Buy useless widgets now</aside>
<footer>Site footer boilerplate</footer></body></html>"""


def _create_mock_config(*, max_chars: int = 12000, timeout: int = 7) -> ConfigManager:
    return ConfigManager(read_max_chars=max_chars, read_timeout_seconds=timeout)


def _create_mock_async_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[..., httpx.AsyncClient]:
    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    return factory


def _create_mock_html_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, text=_ARTICLE, headers={"content-type": "text/html; charset=utf-8"}
    )


def _create_mock_stream(
    chunks: List[bytes], state: Dict[str, Any]
) -> httpx.AsyncByteStream:
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            for chunk in chunks:
                state["chunks_read"] = state.get("chunks_read", 0) + 1
                yield chunk

        async def aclose(self) -> None:
            state["closed"] = True

    return Stream()


def _create_mock_text_result(
    text: str,
    *,
    max_chars: Optional[int] = None,
    offset: Optional[int] = 0,
    configured_max: int = 12000,
) -> ToolResult:
    tool = ReadUrlTool(config=_create_mock_config(max_chars=configured_max))
    with (
        patch(
            "httpx.AsyncClient",
            side_effect=_create_mock_async_client(_create_mock_html_response),
        ),
        patch(
            "kerkov.tools.read_url_tool._extract_content",
            return_value=("A page title", text),
        ),
    ):
        return asyncio.run(
            tool.execute({"url": _URL, "max_chars": max_chars, "offset": offset})
        )


class ReadUrlValidationTest(unittest.TestCase):
    def test_invalid_params_fail_before_network_access(self) -> None:
        invalid_params = [
            {},
            {"url": ""},
            {"url": " \t\n"},
            {"url": "example.com/release"},
            {"url": "file:///etc/passwd"},
            {"url": "ftp://example.com/release"},
            {"url": "https:///release"},
            {"url": "https://exa mple.com/release"},
            {"url": "https://[broken/release"},
            {"url": "https://example.com:invalid/release"},
            {"url": _URL, "offset": -1},
            {"url": _URL, "max_chars": 0},
            {"url": _URL, "max_chars": -1},
        ]
        tool = ReadUrlTool(config=_create_mock_config())
        with patch("httpx.AsyncClient") as client:
            for params in invalid_params:
                with self.subTest(params=params):
                    result = asyncio.run(tool.execute(params))
                    self.assertFalse(result.success)
                    self.assertTrue(result.error)
            client.assert_not_called()

    def test_none_offset_is_zero(self) -> None:
        result = _create_mock_text_result("A short readable page.", offset=None)
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.output, "A short readable page.")
        self.assertEqual(result.metadata["offset"], 0)


class ReadUrlExtractionTest(unittest.TestCase):
    def test_extracts_title_article_and_formatting_without_boilerplate(self) -> None:
        requests: List[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return _create_mock_html_response(request)

        tool = ReadUrlTool(config=_create_mock_config(timeout=9))
        with patch("httpx.AsyncClient", side_effect=_create_mock_async_client(handler)):
            result = asyncio.run(tool.execute({"url": _URL}))

        self.assertTrue(result.success, result.error)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].method, "GET")
        self.assertEqual(str(requests[0].url), _URL)
        self.assertEqual(requests[0].extensions["timeout"]["read"], 9)
        self.assertIn("# Security release notes", result.output)
        self.assertIn("## Upgrade guidance", result.output)
        self.assertIn("- Back up the service configuration", result.output)
        self.assertIn("server --version\nserver --check-config", result.output)
        for boilerplate in (
            "secretScript",
            "secret-style",
            "Site header boilerplate",
            "Navigation only",
            "Buy useless widgets now",
            "Site footer boilerplate",
            "<article>",
        ):
            with self.subTest(boilerplate=boilerplate):
                self.assertNotIn(boilerplate, result.output)
        self.assertEqual(
            result.metadata,
            {
                "url": _URL,
                "resolved_url": _URL,
                "title": "Security release notes",
                "total_length": len(result.output),
                "returned_length": len(result.output),
                "offset": 0,
                "truncated": False,
                "next_offset": None,
            },
        )

    def test_extracts_article_after_a_giant_script_without_raw_html_clipping(
        self,
    ) -> None:
        html = _ARTICLE.replace(
            "const secretScript = 'ignore';",
            "const giantScript = '" + "x" * 50000 + "';",
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=html, headers={"content-type": "text/html"})

        with patch("httpx.AsyncClient", side_effect=_create_mock_async_client(handler)):
            result = asyncio.run(
                ReadUrlTool(config=_create_mock_config(max_chars=1500)).execute(
                    {"url": _URL}
                )
            )

        self.assertTrue(result.success, result.error)
        self.assertIn("The release fixes", result.output)
        self.assertIn("server --check-config", result.output)
        self.assertNotIn("giantScript", result.output)
        self.assertFalse(result.metadata["truncated"])

    def test_redirects_to_xhtml_page_are_followed(self) -> None:
        requests: List[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(str(request.url))
            if request.url.path == "/release":
                return httpx.Response(302, headers={"location": "/latest"})
            return httpx.Response(
                200,
                text=_ARTICLE,
                headers={"content-type": "Application/XHTML+XML; charset=UTF-8"},
            )

        with patch("httpx.AsyncClient", side_effect=_create_mock_async_client(handler)):
            result = asyncio.run(
                ReadUrlTool(config=_create_mock_config()).execute({"url": _URL})
            )

        self.assertTrue(result.success, result.error)
        self.assertEqual(requests, [_URL, "https://example.com/latest"])
        self.assertIn("The release fixes", result.output)
        self.assertEqual(result.metadata["url"], _URL)
        self.assertEqual(result.metadata["resolved_url"], "https://example.com/latest")

    def test_article_links_use_the_full_redirect_destination(self) -> None:
        resolved_url = "https://vendor.example/advisories/2026/note.html"
        links = {
            "proof of concept": (
                "https://example.org/poc.py",
                "https://example.org/poc.py",
            ),
            "CVE entry": (
                "/vuln/CVE-2024-1234",
                "https://vendor.example/vuln/CVE-2024-1234",
            ),
            "patch": (
                "patch.diff",
                "https://vendor.example/advisories/2026/patch.diff",
            ),
            "release notes": (
                "../release.html",
                "https://vendor.example/advisories/release.html",
            ),
            "mitigation": ("#mitigation", resolved_url + "#mitigation"),
            "plain version": ("?format=plain", resolved_url + "?format=plain"),
            "mirror": ("//cdn.example/patch.diff", "https://cdn.example/patch.diff"),
        }
        references = ", ".join(
            f'<a href="{href}">{label}</a>' for label, (href, _) in links.items()
        )
        html = _ARTICLE.replace(
            "</article>", f"<p>References: {references}.</p></article>"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if "text/html" not in request.headers.get("accept", ""):
                return httpx.Response(406)
            if str(request.url) == _URL:
                return httpx.Response(302, headers={"location": resolved_url})
            self.assertEqual(str(request.url), resolved_url)
            return httpx.Response(200, text=html, headers={"content-type": "text/html"})

        with patch("httpx.AsyncClient", side_effect=_create_mock_async_client(handler)):
            result = asyncio.run(
                ReadUrlTool(config=_create_mock_config()).execute({"url": _URL})
            )

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.metadata["url"], _URL)
        self.assertEqual(result.metadata["resolved_url"], resolved_url)
        for label, (_, expected_url) in links.items():
            with self.subTest(label=label):
                self.assertIn(f"[{label}]({expected_url})", result.output)
        self.assertNotIn("Navigation only", result.output)

    def test_article_links_honor_the_document_base(self) -> None:
        url = "https://example.com/advisories/2026/note.html"
        for base, expected_url in (
            ("/downloads/updates/", "https://example.com/downloads/updates/patch.diff"),
            ("../patches/", "https://example.com/advisories/patches/patch.diff"),
            (
                "https://downloads.example/security/",
                "https://downloads.example/security/patch.diff",
            ),
        ):
            with self.subTest(base=base):
                html = _ARTICLE.replace(
                    "</head>", f'<base href="{base}"/></head>'
                ).replace(
                    "</article>",
                    '<p>Download the <a href="patch.diff">vendor patch</a>.</p></article>',
                )

                def handler(request: httpx.Request) -> httpx.Response:
                    return httpx.Response(
                        200, text=html, headers={"content-type": "text/html"}
                    )

                with patch(
                    "httpx.AsyncClient", side_effect=_create_mock_async_client(handler)
                ):
                    result = asyncio.run(
                        ReadUrlTool(config=_create_mock_config()).execute({"url": url})
                    )

                self.assertTrue(result.success, result.error)
                self.assertIn(f"[vendor patch]({expected_url})", result.output)

    def test_redirect_body_is_closed_without_being_downloaded(self) -> None:
        state: Dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/release":
                return httpx.Response(
                    302,
                    headers={"location": "/latest"},
                    stream=_create_mock_stream(
                        [b"x" * (_MAX_RESPONSE_BYTES + 1)], state
                    ),
                )
            return _create_mock_html_response(request)

        with patch("httpx.AsyncClient", side_effect=_create_mock_async_client(handler)):
            result = asyncio.run(
                ReadUrlTool(config=_create_mock_config()).execute({"url": _URL})
            )

        self.assertTrue(result.success, result.error)
        self.assertEqual(state.get("chunks_read", 0), 0)
        self.assertTrue(state["closed"])
        self.assertIn("The release fixes", result.output)

    def test_short_readable_page_is_not_rejected_as_too_small(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text="<html><head><title>Status</title></head>"
                "<body><main><p>All services are operational.</p></main></body></html>",
                headers={"content-type": "text/html"},
            )

        with patch("httpx.AsyncClient", side_effect=_create_mock_async_client(handler)):
            result = asyncio.run(
                ReadUrlTool(config=_create_mock_config()).execute({"url": _URL})
            )
        self.assertTrue(result.success, result.error)
        self.assertIn("All services are operational.", result.output)

    def test_short_document_keeps_headings_and_code_blocks(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text="<html><body><main><h1>Quick start</h1>"
                "<p>Run the diagnostic command:</p>"
                "<pre><code>server --version\nserver --check-config</code></pre>"
                "</main></body></html>",
                headers={"content-type": "text/html"},
            )

        with patch("httpx.AsyncClient", side_effect=_create_mock_async_client(handler)):
            result = asyncio.run(
                ReadUrlTool(config=_create_mock_config()).execute({"url": _URL})
            )

        self.assertTrue(result.success, result.error)
        self.assertIn("# Quick start", result.output)
        self.assertIn(
            "```\nserver --version\nserver --check-config\n```", result.output
        )

    def test_title_metadata_is_bounded_even_when_extractor_returns_a_giant_title(
        self,
    ) -> None:
        document = Document()
        document.title = "T" * 5000
        document.text = "A short readable article."
        with (
            patch(
                "httpx.AsyncClient",
                side_effect=_create_mock_async_client(_create_mock_html_response),
            ),
            patch("kerkov.tools.read_url_tool.bare_extraction", return_value=document),
        ):
            result = asyncio.run(
                ReadUrlTool(config=_create_mock_config()).execute({"url": _URL})
            )

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.output, document.text)
        self.assertTrue(result.metadata["title"])
        self.assertLessEqual(len(result.metadata["title"]), 512)


class ReadUrlClippingTest(unittest.TestCase):
    def test_untruncated_output_retains_every_character(self) -> None:
        content = "One paragraph.\n\nA second paragraph with Unicode: café 🦊."
        result = _create_mock_text_result(content, max_chars=len(content))
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.output, content)
        self.assertEqual(result.metadata["total_length"], len(content))
        self.assertEqual(result.metadata["returned_length"], len(result.output))
        self.assertFalse(result.metadata["truncated"])
        self.assertIsNone(result.metadata.get("next_offset"))

    def test_clipping_counts_marker_and_clamps_to_configured_maximum(self) -> None:
        content = "".join(chr(0x400 + i) for i in range(1000))
        for requested in (None, 100000):
            with self.subTest(max_chars=requested):
                result = _create_mock_text_result(
                    content, max_chars=requested, configured_max=180
                )
                self.assertTrue(result.success, result.error)
                self.assertLessEqual(len(result.output), 180)
                self.assertEqual(result.metadata["returned_length"], len(result.output))
                self.assertEqual(result.metadata["total_length"], len(content))
                self.assertTrue(result.metadata["truncated"])
                marker = _MARKER.search(result.output)
                self.assertIsNotNone(marker, result.output)
                assert marker is not None
                head, tail = (
                    result.output[: marker.start()],
                    result.output[marker.end() :],
                )
                self.assertTrue(head)
                self.assertTrue(tail)
                self.assertTrue(content.startswith(head))
                self.assertTrue(content.endswith(tail))
                self.assertEqual(int(marker[1]), len(content) - len(head) - len(tail))
                self.assertEqual(int(marker[2]), len(content))
                self.assertEqual(int(marker[3]), len(head))
                self.assertEqual(result.metadata["next_offset"], len(head))

    def test_offset_selects_suffix_before_clipping(self) -> None:
        content = "".join(chr(0x400 + i) for i in range(700))
        offset = 173
        result = _create_mock_text_result(content, max_chars=160, offset=offset)
        self.assertTrue(result.success, result.error)
        marker = _MARKER.search(result.output)
        self.assertIsNotNone(marker, result.output)
        assert marker is not None
        head, tail = result.output[: marker.start()], result.output[marker.end() :]
        self.assertTrue(content[offset:].startswith(head))
        self.assertTrue(content.endswith(tail))
        self.assertEqual(int(marker[2]), len(content))
        self.assertEqual(result.metadata["offset"], offset)
        self.assertEqual(result.metadata["total_length"], len(content))
        self.assertEqual(result.metadata["next_offset"], offset + len(head))

    def test_continuation_does_not_skip_the_middle_hidden_by_tail_preview(self) -> None:
        content = "".join(chr(0x400 + i) for i in range(500))
        recovered = ""
        offset = 0
        for _ in range(len(content)):
            result = _create_mock_text_result(content, max_chars=160, offset=offset)
            self.assertTrue(result.success, result.error)
            self.assertLessEqual(len(result.output), 160)
            if not result.metadata["truncated"]:
                self.assertEqual(result.output, content[offset:])
                recovered += result.output
                break
            next_offset = result.metadata["next_offset"]
            self.assertGreater(next_offset, offset)
            head = content[offset:next_offset]
            self.assertTrue(result.output.startswith(head))
            recovered += head
            offset = next_offset
        else:
            self.fail("Continuation never reached the end of the extracted page")
        self.assertEqual(recovered, content)

    def test_tiny_budgets_keep_a_prefix_and_make_continuation_progress(self) -> None:
        content = "abcdefghijklmnopqrstuvwxyz" * 4
        for budget in (2, 3, 10, 50):
            with self.subTest(budget=budget):
                result = _create_mock_text_result(content, max_chars=budget)
                self.assertTrue(result.success, result.error)
                self.assertLessEqual(len(result.output), budget)
                self.assertTrue(result.metadata["truncated"])
                self.assertIn("…", result.output)
                next_offset = result.metadata["next_offset"]
                self.assertGreater(next_offset, 0)
                self.assertTrue(result.output.startswith(content[:next_offset]))

    def test_budget_one_fails_only_when_truncation_is_required(self) -> None:
        result = _create_mock_text_result("A longer page", max_chars=1)
        self.assertFalse(result.success)
        self.assertTrue(result.error)
        result = _create_mock_text_result("A", max_chars=1)
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.output, "A")

    def test_offset_at_or_beyond_end_returns_empty_success(self) -> None:
        content = "A readable page"
        for offset in (len(content), len(content) + 100):
            with self.subTest(offset=offset):
                result = _create_mock_text_result(content, offset=offset)
                self.assertTrue(result.success, result.error)
                self.assertEqual(result.output, "")
                self.assertEqual(result.metadata["returned_length"], 0)
                self.assertEqual(result.metadata["total_length"], len(content))
                self.assertEqual(result.metadata["offset"], offset)
                self.assertFalse(result.metadata["truncated"])
                self.assertIsNone(result.metadata.get("next_offset"))


class ReadUrlErrorTest(unittest.TestCase):
    def test_redirect_loop_returns_failure_after_a_bounded_number_of_requests(
        self,
    ) -> None:
        requests: List[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            self.assertLess(len(requests), 30, "Redirect loop was not bounded")
            return httpx.Response(302, headers={"location": "/release"})

        with (
            patch("httpx.AsyncClient", side_effect=_create_mock_async_client(handler)),
            patch("kerkov.tools.read_url_tool._extract_content") as extract,
        ):
            result = asyncio.run(
                ReadUrlTool(config=_create_mock_config()).execute({"url": _URL})
            )

        self.assertFalse(result.success)
        self.assertIn("redirect", (result.error or "").lower())
        self.assertLess(len(requests), 30)
        extract.assert_not_called()

    def test_timeout_and_connection_errors_return_failures(self) -> None:
        for error_type in (httpx.ReadTimeout, httpx.ConnectError):
            with self.subTest(error_type=error_type):

                def handler(request: httpx.Request) -> httpx.Response:
                    raise error_type("simulated request failure", request=request)

                with patch(
                    "httpx.AsyncClient", side_effect=_create_mock_async_client(handler)
                ):
                    result = asyncio.run(
                        ReadUrlTool(config=_create_mock_config()).execute({"url": _URL})
                    )
                self.assertFalse(result.success)
                self.assertTrue(result.error)
                self.assertEqual(result.output, "")

    def test_http_errors_report_status_without_extraction(self) -> None:
        for status in (404, 503):
            with self.subTest(status=status):

                def handler(request: httpx.Request) -> httpx.Response:
                    return httpx.Response(status, text="Failed upstream")

                with (
                    patch(
                        "httpx.AsyncClient",
                        side_effect=_create_mock_async_client(handler),
                    ),
                    patch("kerkov.tools.read_url_tool._extract_content") as extract,
                ):
                    result = asyncio.run(
                        ReadUrlTool(config=_create_mock_config()).execute({"url": _URL})
                    )
                self.assertFalse(result.success)
                self.assertIn(str(status), result.error or "")
                extract.assert_not_called()

    def test_missing_and_unsupported_mime_types_are_rejected(self) -> None:
        for mime in (
            None,
            "application/pdf",
            "application/json",
            "image/png",
            "text/plain",
        ):
            with self.subTest(mime=mime):

                def handler(request: httpx.Request) -> httpx.Response:
                    headers = {} if mime is None else {"content-type": mime}
                    return httpx.Response(
                        200, content=_ARTICLE.encode(), headers=headers
                    )

                with (
                    patch(
                        "httpx.AsyncClient",
                        side_effect=_create_mock_async_client(handler),
                    ),
                    patch("kerkov.tools.read_url_tool._extract_content") as extract,
                ):
                    result = asyncio.run(
                        ReadUrlTool(config=_create_mock_config()).execute({"url": _URL})
                    )
                self.assertFalse(result.success)
                self.assertTrue(result.error)
                extract.assert_not_called()

    def test_empty_extraction_and_extractor_errors_return_failures(self) -> None:
        for outcome in (
            {"return_value": None},
            {"side_effect": ValueError("bad HTML")},
        ):
            with self.subTest(outcome=outcome):
                with (
                    patch(
                        "httpx.AsyncClient",
                        side_effect=_create_mock_async_client(
                            _create_mock_html_response
                        ),
                    ),
                    patch(
                        "kerkov.tools.read_url_tool.bare_extraction",
                        **outcome,
                    ),
                ):
                    result = asyncio.run(
                        ReadUrlTool(config=_create_mock_config()).execute({"url": _URL})
                    )
                self.assertFalse(result.success)
                self.assertTrue(result.error)
                self.assertEqual(result.output, "")

    def test_stream_limit_stops_download_without_content_length(self) -> None:
        state: Dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                stream=_create_mock_stream(
                    [b"x" * (_MAX_RESPONSE_BYTES + 65536), b"unread remainder"], state
                ),
            )

        with (
            patch("httpx.AsyncClient", side_effect=_create_mock_async_client(handler)),
            patch("kerkov.tools.read_url_tool._extract_content") as extract,
        ):
            result = asyncio.run(
                ReadUrlTool(config=_create_mock_config()).execute({"url": _URL})
            )
        self.assertFalse(result.success)
        self.assertTrue(result.error)
        self.assertEqual(state["chunks_read"], 1)
        self.assertTrue(state["closed"])
        extract.assert_not_called()

    def test_compressed_response_limit_applies_to_decompressed_bytes(self) -> None:
        compressed = gzip.compress(b"x" * (_MAX_RESPONSE_BYTES + 1))
        state: Dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={
                    "content-type": "text/html",
                    "content-encoding": "gzip",
                    "content-length": str(len(compressed)),
                },
                stream=_create_mock_stream([compressed], state),
            )

        with (
            patch("httpx.AsyncClient", side_effect=_create_mock_async_client(handler)),
            patch("kerkov.tools.read_url_tool._extract_content") as extract,
        ):
            result = asyncio.run(
                ReadUrlTool(config=_create_mock_config()).execute({"url": _URL})
            )
        self.assertFalse(result.success)
        self.assertTrue(result.error)
        self.assertTrue(state["closed"])
        extract.assert_not_called()


if __name__ == "__main__":
    unittest.main()
