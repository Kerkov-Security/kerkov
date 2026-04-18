import json
import platform
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import httpx

from src.core.config_manager import ConfigManager
from src.models.basic_models import LLMResponse, Message, TokenUsage, ToolCall
from src.providers.base_provider import BaseLLMProvider
from src.providers.codex.auth import CodexAuthConfig, CodexTokenManager, extract_account_id_from_jwt

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
                auth_config=CodexAuthConfig(
                    state_path=Path(config.codex_state_path),
                    token_url=config.codex_oauth_token_url or "",
                    client_id=config.codex_oauth_client_id,
                    client_secret=config.codex_oauth_client_secret,
                    timeout_seconds=config.max_timeout_seconds,
                ),
                access_token=access_token,
                refresh_token=config.codex_oauth_refresh_token,
                account_id=self.account_id,
                expires_at=config.codex_token_expires_at,
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
        tool_call_index_by_id: Dict[str, int] = {}
        raw_output_items_by_id: Dict[str, Dict[str, Any]] = {}
        completed_response: Optional[Dict[str, Any]] = None

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
                        elif event_type == "response.completed":
                            response_obj = event.get("response")
                            if isinstance(response_obj, dict):
                                completed_response = response_obj
                            self._collect_tool_calls_from_event(
                                event,
                                tool_calls,
                                tool_call_index_by_id,
                                raw_output_items_by_id,
                            )
                        elif event_type in ("response.output_item.added", "response.output_item.done"):
                            self._collect_tool_calls_from_event(
                                event,
                                tool_calls,
                                tool_call_index_by_id,
                                raw_output_items_by_id,
                            )
                    break

        content = "".join(content_parts).strip()
        usage = self._extract_usage(completed_response)
        raw_response = completed_response.copy() if isinstance(completed_response, dict) else {}
        if raw_output_items_by_id:
            raw_response["output"] = list(raw_output_items_by_id.values())
        return LLMResponse(
            content=content,
            model=self.model,
            usage=usage,
            tool_calls=tool_calls if tool_calls else None,
            raw_response=raw_response or completed_response,
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
                if message.call_id:
                    items.append(
                        {
                            "type": "function_call_output",
                            "call_id": message.call_id,
                            "output": message.content,
                        }
                    )
                else:
                    items.append(
                        {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": f"Tool result:\n{message.content}"}],
                        }
                    )
                continue
            if message.role in ("user", "assistant"):
                if message.raw_blocks and isinstance(message.raw_blocks, list):
                    for block in message.raw_blocks:
                        if isinstance(block, dict):
                            items.append(block)
                    continue

                items.append(
                    {
                        "type": "message",
                        "role": message.role,
                        "content": [
                            {
                                "type": "input_text" if message.role == "user" else "output_text",
                                "text": message.content,
                            }
                        ],
                    }
                )
        return items

    def _convert_tool(self, tool: "BaseTool") -> Dict[str, Any]:
        schema = tool.get_generic_schema().model_dump()
        return {
            "type": "function",
            "name": schema.get("name"),
            "description": schema.get("description"),
            "parameters": schema.get("parameters", {}),
        }

    def _collect_tool_calls_from_event(
        self,
        event: Dict[str, Any],
        tool_calls: List[ToolCall],
        tool_call_index_by_id: Dict[str, int],
        raw_output_items_by_id: Dict[str, Dict[str, Any]],
    ) -> None:
        for item in self._iter_function_call_items(event):
            tool_call = self._tool_call_from_item(item)
            if tool_call is None:
                continue

            dedupe_key = tool_call.call_id or f"{tool_call.tool_name}:{json.dumps(tool_call.parameters, sort_keys=True)}"
            existing_index = tool_call_index_by_id.get(dedupe_key)
            if existing_index is None:
                tool_call_index_by_id[dedupe_key] = len(tool_calls)
                tool_calls.append(tool_call)
            else:
                existing = tool_calls[existing_index]
                if not existing.parameters and tool_call.parameters:
                    tool_calls[existing_index] = tool_call

            existing_item = raw_output_items_by_id.get(dedupe_key)
            if existing_item is None:
                raw_output_items_by_id[dedupe_key] = dict(item)
            elif not self._item_has_arguments(existing_item) and self._item_has_arguments(item):
                raw_output_items_by_id[dedupe_key] = dict(item)

    def _iter_function_call_items(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []

        event_item = event.get("item")
        if isinstance(event_item, dict):
            items.append(event_item)

        response_obj = event.get("response")
        if isinstance(response_obj, dict):
            output = response_obj.get("output")
            if isinstance(output, list):
                items.extend(item for item in output if isinstance(item, dict))

        return items

    def _tool_call_from_item(self, item: Dict[str, Any]) -> Optional[ToolCall]:
        if item.get("type") != "function_call":
            return None

        name = item.get("name")
        if not name:
            return None

        args: Dict[str, Any] = {}
        raw_args = item.get("arguments")
        if isinstance(raw_args, str) and raw_args.strip():
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError:
                args = {}
        elif isinstance(raw_args, dict):
            args = raw_args

        return ToolCall(
            tool_name=name,
            parameters=args,
            call_id=item.get("call_id") or item.get("id"),
        )

    def _item_has_arguments(self, item: Dict[str, Any]) -> bool:
        raw_args = item.get("arguments")
        if isinstance(raw_args, dict):
            return bool(raw_args)
        if isinstance(raw_args, str):
            return bool(raw_args.strip())
        return False

    def _extract_usage(self, response_obj: Optional[Dict[str, Any]]) -> Optional[TokenUsage]:
        if not isinstance(response_obj, dict):
            return None

        usage = response_obj.get("usage")
        if not isinstance(usage, dict):
            return None

        input_tokens = int(usage.get("input_tokens", 0) or 0)
        output_tokens = int(usage.get("output_tokens", 0) or 0)
        total_tokens = int(usage.get("total_tokens", input_tokens + output_tokens) or 0)

        return TokenUsage(
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            total_tokens=total_tokens,
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
                "Missing ChatGPT account id in token. Re-run /codex-login to refresh Codex runtime auth state."
            )
        self.api_key = self.access_token
