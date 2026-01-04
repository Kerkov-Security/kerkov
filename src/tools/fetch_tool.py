"""
Web content fetching tool with security controls.
"""

from typing import Dict, Optional
import httpx
from pydantic import Field

from src.core.config_manager import get_config
from src.tools.base_tool import BaseTool, ToolParams, ToolResult

class FetchToolParams(ToolParams):
    url: str = Field(description="URL to fetch (must start with http:// or https://)")
    method: str = Field(default="GET", description="HTTP method (GET or POST, default: GET)")
    headers: Optional[Dict[str, str]] = Field(default=None, description="Optional HTTP headers as key-value pairs")
    data: Optional[str] = Field(default=None, description="Optional request body for POST requests",)
    timeout: Optional[float] = Field(default=None, description="Request timeout in seconds (optional)")

class FetchTool(BaseTool):
    """Fetch content from URLs."""

    # Max response size to read (10 MB)
    MAX_RESPONSE_SIZE = 10 * 1024 * 1024

    SUPPORTED_HTTP_METHODS = ["GET", "POST"]

    def __init__(self):
        super().__init__(
            name="fetch",
            description="Fetch content from a URL using HTTP/HTTPS. Supports GET and POST requests. Returns the response body as text. Has a maximum response size limit of 10MB for safety.",
        )
        self.config = get_config()

    def get_params_class(self):
        return FetchToolParams

    def validate_params(self, toolParams: FetchToolParams) -> str:
        url = toolParams.url.strip()

        invalidUrlMsg = "URL must start with http:// or https://, and method must be GET or POST"
        if not url:
            return invalidUrlMsg
        if not (url.startswith("http://") or url.startswith("https://")):
            return invalidUrlMsg
        
        method = toolParams.method.upper()
        if method not in self.SUPPORTED_HTTP_METHODS:
            return f"HTTP methods currently supported: {self.SUPPORTED_HTTP_METHODS}"
        
        if toolParams.timeout and toolParams.timeout < 0:
            return f"Timeout must be at least greater than 0.0"
        
        return None

    async def run(self, toolParams: FetchToolParams) -> ToolResult:
        method = toolParams.method.upper()
        request_timeout = toolParams.timeout or self.config.max_timeout_seconds

        try:
            request_headers = toolParams.headers or {}
            # Need to determine if we can skip this
            # if "User-Agent" not in request_headers:
            #     request_headers["User-Agent"] = "Kerkov-Bot/1.0" # Maybe we should not specify this?

            async with httpx.AsyncClient(timeout=request_timeout) as client:
                if method == "GET":
                    response = await client.get(toolParams.url, headers=request_headers)
                else:  # POST
                    response = await client.post(toolParams.url, headers=request_headers, content=toolParams.data)

                return await self._process_response(response, toolParams.url)

        except httpx.TimeoutException:
            return ToolResult(
                success=False,
                output="",
                error=f"Request timed out after {request_timeout} seconds",
                metadata={"url": toolParams.url, "timeout": request_timeout},
            )
        except httpx.HTTPError as e:
            return ToolResult(
                success=False,
                output="",
                error=f"HTTP error: {str(e)}",
                metadata={"url": toolParams.url},
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Fetch error: {str(e)}",
                metadata={"url": toolParams.url},
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

            content = response.text

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
        