"""DeepSeek provider using its OpenAI-compatible chat API."""

from typing import Any, Dict, List

import httpx

from kerkov.models.basic_models import Message
from kerkov.providers.openai_compatible import OpenAICompatibleProvider

_MODEL_LIST_TIMEOUT_SECONDS = 10


class DeepSeekProvider(OpenAICompatibleProvider):
    """Hosted inference through DeepSeek API endpoints."""

    caches_prompt_prefix = True
    api_label = "DeepSeek"
    empty_assistant_content = ""
    replays_reasoning = True

    def _adjust_payload(self, payload: Dict[str, Any]) -> None:
        if "tools" not in payload:
            return
        # Thinking with tools requires reasoning for every assistant turn,
        # including turns without calls. Keep imported conversations usable
        # in non-thinking mode until incompatible history is compacted away.
        if any(
            message["role"] == "assistant" and "reasoning_content" not in message
            for message in payload["messages"]
        ):
            payload["thinking"] = {"type": "disabled"}
            for message in payload["messages"]:
                message.pop("reasoning_content", None)

    def validate_connection(self) -> bool:
        try:
            with httpx.Client(
                base_url=self.base_url,
                headers=self._headers,
                timeout=self.timeout_seconds,
            ) as client:
                response = client.get("/models")
                self._raise_for_status(response)
            return True
        except Exception as exc:
            self.logger.error(f"DeepSeek connection validation failed: {exc}")
            return False

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
            self.logger.error(f"Failed to list DeepSeek models: {exc}")
            return []

    def _cache_read_tokens(self, usage_data: Dict[str, Any]) -> Any:
        cached_tokens = usage_data.get("prompt_cache_hit_tokens")
        if cached_tokens is None:
            return super()._cache_read_tokens(usage_data)
        return cached_tokens

    def _convert_message(self, msg: Message) -> List[Dict[str, Any]]:
        if msg.summary_level is not None:
            # Compactor summaries are supplied context, not model turns
            # with replayable reasoning. Keep them out of the assistant
            # history so compaction alone never disables thinking.
            return [{"role": "user", "content": msg.content}]
        return super()._convert_message(msg)

    @classmethod
    def _raise_for_status(cls, response: httpx.Response) -> None:
        if response.status_code == 402:
            raise ValueError(
                "DeepSeek API error 402: Insufficient balance. "
                "Please top up your DeepSeek account."
            )
        super()._raise_for_status(response)
