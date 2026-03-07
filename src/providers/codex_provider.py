import json
import platform
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import httpx

from src.core.config_manager import ConfigManager
from src.models.basic_models import LLMResponse, Message, ToolCall
from src.providers.base_provider import BaseLLMProvider
from src.providers.codex_auth import CodexTokenManager, extract_account_id_from_jwt

if TYPE_CHECKING:
    from src.tools.base_tool import BaseTool


class CodexProvider(BaseLLMProvider):
    """Codex provider using ChatGPT backend responses endpoint."""

    def __init__(
        self,
        model: str,
        base_url: str,
        access_token: Optional[str],
        account_id: Optional[str] = None,
        session_id: Optional[str] = None,
        timeout_seconds: int = 60,
        config: Optional[ConfigManager] = None,
    ):
        super().__init__(api_key=access_token or "", model=model)
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self.timeout_seconds = timeout_seconds
        self.session_id = session_id
        self.account_id = account_id or extract_account_id_from_jwt(access_token or "")
        self.token_manager = (
            CodexTokenManager(
                config=config,
                access_token=access_token,
                account_id=self.account_id,
            )
            if config
            else None
        )

    async def complete(
        self,
        messages: List[Message],
        temperature: float,
        max_tokens: Optional[int] = None,
        tools: Optional[List["BaseTool"]] = None,
    ) -> LLMResponse:
        self._ensure_auth()

        instructions = self._extract_instructions(messages)
        input_items = self._convert_messages_to_responses_input(messages)

        payload: Dict[str, Any] = {
            "model": self.model,
            "store": False,
            "stream": True,
            "instructions": instructions,
            "input": input_items,
            "text": {"verbosity": "medium"},
            "include": ["reasoning.encrypted_content"],
            "tool_choice": "auto",
            "parallel_tool_calls": True,
        }

        if self.session_id:
            payload["prompt_cache_key"] = self.session_id

        if tools:
            payload["tools"] = [self._convert_tool(tool) for tool in tools]

        url = self._resolve_responses_url()
        content_parts: List[str] = []
        tool_calls: List[ToolCall] = []

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            has_retried_after_refresh = False
            while True:
                headers = self._build_headers()
                async with client.stream("POST", url, headers=headers, json=payload) as response:
                    if response.status_code == 401 and self.token_manager and not has_retried_after_refresh:
                        await response.aread()
                        self._ensure_auth(force_refresh=True)
                        has_retried_after_refresh = True
                        continue
                    if response.status_code >= 400:
                        raise ValueError(
                            f"Codex backend error {response.status_code}: {await response.aread()}"
                        )

                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            event = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        event_type = event.get("type")
                        if event_type in (
                            "response.output_text.delta",
                            "response.content_part.added",
                        ):
                            delta = (
                                event.get("delta")
                                or event.get("text")
                                or event.get("content", {}).get("text")
                                or ""
                            )
                            if delta:
                                content_parts.append(str(delta))
                        elif event_type in ("response.completed", "response.output_item.done"):
                            self._collect_tool_calls_from_event(event, tool_calls)
                    break

        content = "".join(content_parts).strip()
        return LLMResponse(
            content=content,
            model=self.model,
            tool_calls=tool_calls if tool_calls else None,
        )

    async def count_tokens(self, text: str) -> int:
        return max(1, len(text.split()))

    def validate_connection(self) -> bool:
        if self.token_manager:
            return self.token_manager.validate_available_auth()
        return bool(self.access_token and self.account_id)

    def list_models(self) -> List[str]:
        # ChatGPT backend does not expose a models-list endpoint compatible with this flow.
        return [self.model]

    def _resolve_responses_url(self) -> str:
        # Handles base forms:
        # - https://chatgpt.com/backend-api
        # - https://chatgpt.com/backend-api/codex
        # - https://chatgpt.com/backend-api/codex/responses
        base = self.base_url.rstrip("/")
        if base.endswith("/codex/responses"):
            return base
        if base.endswith("/codex"):
            return f"{base}/responses"
        return f"{base}/codex/responses"

    def _build_headers(self) -> Dict[str, str]:
        machine = platform.machine().lower() or "unknown"
        system = platform.system().lower() or "unknown"
        release = platform.release().lower() or "unknown"
        user_agent = f"pi ({system}/{release}/{machine})"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "chatgpt-account-id": self.account_id or "",
            "OpenAI-Beta": "responses=experimental",
            "originator": "pi",
            "User-Agent": user_agent,
            "accept": "text/event-stream",
            "content-type": "application/json",
        }
        if self.session_id:
            headers["session_id"] = self.session_id
        return headers

    def _extract_instructions(self, messages: List[Message]) -> str:
        system_messages = [m.content for m in messages if m.role == "system" and m.content]
        return "\n\n".join(system_messages).strip()

    def _convert_messages_to_responses_input(self, messages: List[Message]) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for message in messages:
            if message.role == "system":
                continue
            if message.role == "tool":
                items.append({"role": "user", "content": f"Tool result:\n{message.content}"})
                continue
            if message.role in ("user", "assistant"):
                items.append({"role": message.role, "content": message.content})
        return items

    def _convert_tool(self, tool: "BaseTool") -> Dict[str, Any]:
        schema = tool.get_generic_schema().model_dump()
        return {
            "type": "function",
            "name": schema.get("name"),
            "description": schema.get("description"),
            "parameters": schema.get("parameters", {}),
        }

    def _collect_tool_calls_from_event(self, event: Dict[str, Any], tool_calls: List[ToolCall]) -> None:
        response_obj = event.get("response")
        if not isinstance(response_obj, dict):
            return
        output = response_obj.get("output")
        if not isinstance(output, list):
            return

        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "function_call":
                continue

            name = item.get("name")
            if not name:
                continue

            args = {}
            raw_args = item.get("arguments")
            if isinstance(raw_args, str) and raw_args.strip():
                try:
                    args = json.loads(raw_args)
                except json.JSONDecodeError:
                    args = {}
            elif isinstance(raw_args, dict):
                args = raw_args

            tool_calls.append(
                ToolCall(
                    tool_name=name,
                    parameters=args,
                    call_id=item.get("call_id") or item.get("id"),
                )
            )

    def _ensure_auth(self, force_refresh: bool = False) -> None:
        if self.token_manager:
            self.access_token = self.token_manager.ensure_valid_token(force_refresh=force_refresh)
            self.account_id = self.token_manager.account_id
            self.api_key = self.access_token
            return
        if not self.access_token:
            raise ValueError("Missing Codex access token. Run /codex-login.")
        if not self.account_id:
            self.account_id = extract_account_id_from_jwt(self.access_token)
        if not self.account_id:
            raise ValueError(
                "Missing ChatGPT account id in token. Re-run /codex-login to refresh CODEX_ACCESS_TOKEN."
            )
        self.api_key = self.access_token
