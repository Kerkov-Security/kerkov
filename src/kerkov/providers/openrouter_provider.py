"""OpenRouter provider using its OpenAI-compatible chat API."""

from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import httpx

from kerkov.providers.openai_compatible import OpenAICompatibleProvider

_MODEL_LIST_TIMEOUT_SECONDS = 10


class OpenRouterProvider(OpenAICompatibleProvider):
    """Hosted inference through OpenRouter API endpoints."""

    api_label = "OpenRouter"

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        http_referer: Optional[str] = None,
        app_title: Optional[str] = None,
        timeout_seconds: int = 60,
    ) -> None:
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            model=model,
            timeout_seconds=timeout_seconds,
        )
        self.http_referer = http_referer
        self.app_title = app_title
        self._context_window_cache: Dict[str, Optional[int]] = {}
        self._max_output_cache: Dict[str, Optional[int]] = {}

    @property
    def _headers(self) -> Dict[str, str]:
        headers = super()._headers
        if self.http_referer:
            headers["HTTP-Referer"] = self.http_referer
        if self.app_title:
            headers["X-OpenRouter-Title"] = self.app_title
        return headers

    def validate_connection(self) -> bool:
        try:
            hosted_openrouter = self._is_hosted_openrouter()
            with httpx.Client(
                base_url=self.base_url,
                headers=self._headers,
                timeout=self.timeout_seconds,
            ) as client:
                if hosted_openrouter:
                    # The hosted catalog is public; /key validates credentials
                    # without generating billable model output.
                    response = client.get("/key")
                else:
                    # Startup checks must remain cheap and model-independent.
                    response = client.get("/models")
                response.raise_for_status()
                data = response.json()
            expected = dict if hosted_openrouter else list
            return isinstance(data, dict) and isinstance(data.get("data"), expected)
        except Exception as exc:
            self.logger.error(
                "OpenRouter connection validation failed (%s)", type(exc).__name__
            )
            return False

    def validate_model(self) -> bool:
        if self._is_hosted_openrouter():
            return self.validate_connection()
        try:
            # A custom catalog can be public. Explicit model selection/setup
            # verifies authenticated inference without sending engagement history.
            with httpx.Client(
                base_url=self.base_url,
                headers=self._headers,
                timeout=self.timeout_seconds,
            ) as client:
                response = client.post(
                    "/chat/completions",
                    json={
                        "model": self.model,
                        "messages": [{"role": "user", "content": "Hi"}],
                        "max_tokens": 1,
                        "stream": False,
                    },
                )
                response.raise_for_status()
                data = response.json()
            choices = data.get("choices") if isinstance(data, dict) else None
            if not isinstance(choices, list) or not choices:
                return False
            message = (
                choices[0].get("message") if isinstance(choices[0], dict) else None
            )
            return (
                isinstance(message, dict)
                and message.get("role") == "assistant"
                and (
                    isinstance(message.get("content"), str)
                    or ("content" in message and message["content"] is None)
                )
            )
        except Exception as exc:
            self.logger.error(
                "OpenRouter model validation failed (%s)", type(exc).__name__
            )
            return False

    def _is_hosted_openrouter(self) -> bool:
        """Only the canonical API origin and prefix guarantee the /key route."""
        url = urlsplit(self.base_url)
        return (
            url.scheme == "https"
            and url.hostname == "openrouter.ai"
            and url.port in (None, 443)
            and url.path.rstrip("/") == "/api/v1"
            and url.username is None
            and url.password is None
            and not url.query
            and not url.fragment
        )

    def list_models(self) -> List[str]:
        try:
            with httpx.Client(
                base_url=self.base_url,
                headers=self._headers,
                timeout=min(self.timeout_seconds, _MODEL_LIST_TIMEOUT_SECONDS),
            ) as client:
                response = client.get("/models")
                self._raise_for_status(response)
                data = response.json()
            return [item["id"] for item in data.get("data", []) if item.get("id")]
        except Exception as exc:
            self.logger.error(f"Failed to list OpenRouter models: {exc}")
            return []

    async def get_max_context_tokens(self) -> Optional[int]:
        if self.model in self._context_window_cache:
            return self._context_window_cache[self.model]
        await self._populate_model_token_caches()
        return self._context_window_cache.get(self.model)

    async def _populate_model_token_caches(self) -> None:
        context_window: Optional[int] = None
        try:
            metadata = await self._fetch_model_metadata()
            context_window, _ = self._extract_model_token_limits(metadata)
        except Exception as exc:
            self.logger.warning(
                f"Failed to retrieve OpenRouter model token limits: {exc}"
            )
        self._context_window_cache[self.model] = context_window
        # Avoid sending OpenRouter's largest provider cap as per-turn
        # max_tokens.
        self._max_output_cache[self.model] = None

    async def _fetch_model_metadata(self) -> Dict[str, Any]:
        author, slug = self._split_model_slug(self.model)
        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._headers,
            timeout=self.timeout_seconds,
        ) as client:
            response = await client.get(f"/model/{author}/{slug}")
            self._raise_for_status(response)
            data = response.json()

        model_data = data.get("data") if isinstance(data, dict) else None
        return model_data if isinstance(model_data, dict) else data

    @staticmethod
    def _split_model_slug(model: str) -> Tuple[str, str]:
        if "/" not in model:
            raise ValueError(
                "OpenRouter model must be an author/slug pair: " f"{model}"
            )
        author, slug = model.split("/", 1)
        if not author or not slug:
            raise ValueError(
                "OpenRouter model must be an author/slug pair: " f"{model}"
            )
        return author, slug

    @staticmethod
    def _extract_model_token_limits(
        metadata: Dict[str, Any],
    ) -> Tuple[Optional[int], Optional[int]]:
        top_provider = metadata.get("top_provider")
        if not isinstance(top_provider, dict):
            top_provider = {}

        context_window = OpenRouterProvider._coerce_optional_int(
            metadata.get("context_length") or top_provider.get("context_length")
        )
        max_output = OpenRouterProvider._coerce_optional_int(
            top_provider.get("max_completion_tokens")
        )
        return context_window, max_output

    @staticmethod
    def _coerce_optional_int(value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
