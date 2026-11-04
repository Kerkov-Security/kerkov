"""NVIDIA NIM provider using its OpenAI-compatible chat API."""

from typing import List

import httpx

from kerkov.providers.openai_compatible import OpenAICompatibleProvider


class NvidiaProvider(OpenAICompatibleProvider):
    """Hosted inference through NVIDIA NIM API endpoints."""

    api_label = "NVIDIA NIM"

    def validate_connection(self) -> bool:
        """Check server reachability without requiring the default chat model."""
        try:
            with httpx.Client(
                base_url=self.base_url,
                headers=self._headers,
                timeout=self.timeout_seconds,
            ) as client:
                response = client.get("/models")
                response.raise_for_status()
            return True
        except Exception as exc:
            status = (
                exc.response.status_code
                if isinstance(exc, httpx.HTTPStatusError)
                else type(exc).__name__
            )
            self.logger.error("NVIDIA NIM connection validation failed (%s)", status)
            return False

    def validate_model(self) -> bool:
        try:
            with httpx.Client(
                base_url=self.base_url,
                headers=self._headers,
                timeout=self.timeout_seconds,
            ) as client:
                # NVIDIA's model catalog is public. Validate authenticated
                # access to the selected model with a minimal inference call.
                # This may consume tokens, but never sends engagement history.
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
            return (
                isinstance(choices, list)
                and bool(choices)
                and isinstance(choices[0], dict)
                and isinstance(choices[0].get("message"), dict)
            )
        except Exception as exc:
            self.logger.error(
                "NVIDIA NIM connection validation failed (%s)", type(exc).__name__
            )
            return False

    def list_models(self) -> List[str]:
        try:
            with httpx.Client(
                base_url=self.base_url,
                headers=self._headers,
                timeout=self.timeout_seconds,
            ) as client:
                response = client.get("/models")
                self._raise_for_status(response)
                data = response.json()
            return [item["id"] for item in data.get("data", []) if item.get("id")]
        except Exception as exc:
            self.logger.error(f"Failed to list NVIDIA NIM models: {exc}")
            return []
