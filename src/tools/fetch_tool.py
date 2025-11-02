"""
Web content fetching tool with security controls.
"""

from typing import Dict, Optional
import httpx

from src.core.config_manager import get_config
from src.tools.base_tool import BaseTool, ToolResult


class FetchTool(BaseTool):
    """Fetch content from URLs with security controls."""

    # Max response size to read (10 MB)
    MAX_RESPONSE_SIZE = 10 * 1024 * 1024

    def __init__(self):
        """Initialize the fetch tool."""
        super().__init__(
            name="fetch",
            description="Fetch content from a URL using HTTP/HTTPS. Supports GET and POST requests. Returns the response body as text. Has a maximum response size limit of 10MB for safety.",
        )
        self.config = get_config()

    def validate_params(self, **kwargs) -> bool:
        """
        Validate fetch tool parameters.

        Args:
            **kwargs: Must contain 'url' parameter

        Returns:
            True if parameters are valid
        """
        if "url" not in kwargs:
            return False
        if not isinstance(kwargs["url"], str):
            return False
        if not kwargs["url"].strip():
            return False
        # Basic URL validation
        url = kwargs["url"].strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            return False
        return True

    async def execute(
        self,
        url: str,
        method: str = "GET",
        headers: Optional[Dict[str, str]] = None,
        data: Optional[str] = None,
        timeout: Optional[int] = None,
        **kwargs
    ) -> ToolResult:
        """
        Fetch content from a URL.

        Args:
            url: URL to fetch
            method: HTTP method (GET or POST, default: GET)
            headers: Optional HTTP headers dictionary
            data: Optional request body for POST requests
            timeout: Request timeout in seconds (default from config)
            **kwargs: Additional parameters

        Returns:
            ToolResult with response content
        """
        if not self.validate_params(url=url):
            return ToolResult(
                success=False,
                output="",
                error="Invalid URL: must be a non-empty string starting with http:// or https://",
            )

        # Validate method
        method = method.upper()
        if method not in ["GET", "POST"]:
            return ToolResult(
                success=False,
                output="",
                error=f"Unsupported HTTP method: {method}. Only GET and POST are supported.",
            )

        request_timeout = timeout or self.config.max_timeout_seconds

        try:
            # Prepare headers
            request_headers = headers or {}
            if "User-Agent" not in request_headers:
                request_headers["User-Agent"] = "Kerkov-Bot/1.0"

            # Create httpx client with timeout
            async with httpx.AsyncClient(timeout=request_timeout) as client:
                # Make the request
                if method == "GET":
                    response = await client.get(url, headers=request_headers)
                else:  # POST
                    response = await client.post(url, headers=request_headers, content=data)

                return await self._process_response(response, url)

        except httpx.TimeoutException:
            return ToolResult(
                success=False,
                output="",
                error=f"Request timed out after {request_timeout} seconds",
                metadata={"url": url, "timeout": request_timeout},
            )
        except httpx.HTTPError as e:
            return ToolResult(
                success=False,
                output="",
                error=f"HTTP error: {str(e)}",
                metadata={"url": url},
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Fetch error: {str(e)}",
                metadata={"url": url},
            )

    async def _process_response(self, response: httpx.Response, url: str) -> ToolResult:
        """
        Process HTTP response.

        Args:
            response: httpx response object
            url: Original URL

        Returns:
            ToolResult with response data
        """
        try:
            # Check content length from headers
            content_length_header = response.headers.get("content-length")
            if content_length_header:
                try:
                    content_length = int(content_length_header)
                    if content_length > self.MAX_RESPONSE_SIZE:
                        return ToolResult(
                            success=False,
                            output="",
                            error=f"Response too large: {content_length} bytes (max: {self.MAX_RESPONSE_SIZE} bytes)",
                            metadata={
                                "url": url,
                                "status": response.status_code,
                                "content_length": content_length,
                            },
                        )
                except ValueError:
                    pass  # Invalid content-length header, proceed anyway

            # Read response body
            content = response.text

            # Check if we read too much
            if len(content) > self.MAX_RESPONSE_SIZE:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Response too large: {len(content)} bytes (max: {self.MAX_RESPONSE_SIZE} bytes)",
                    metadata={
                        "url": url,
                        "status": response.status_code,
                        "content_length": len(content),
                    },
                )

            # Check HTTP status
            success = 200 <= response.status_code < 300

            return ToolResult(
                success=success,
                output=content,
                error=None if success else f"HTTP {response.status_code}: {response.reason_phrase}",
                metadata={
                    "url": url,
                    "status": response.status_code,
                    "content_type": response.headers.get("content-type"),
                    "content_length": len(content),
                    "headers": dict(response.headers),
                },
            )

        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Error processing response: {str(e)}",
                metadata={"url": url, "status": response.status_code},
            )
