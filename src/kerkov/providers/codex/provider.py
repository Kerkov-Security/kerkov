import asyncio
import json
import platform
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

import httpx

from kerkov.core.config_manager import ConfigManager
from kerkov.models.basic_models import LLMResponse, Message, TokenUsage, ToolCall
from kerkov.providers.base_provider import BaseLLMProvider
from kerkov.providers.codex.auth import (
    CodexAuthConfig,
    CodexTokenManager,
    extract_account_id_from_jwt,
)
from kerkov.providers.replay import replay_tool_calls

if TYPE_CHECKING:
    from kerkov.tools.base_tool import BaseTool


# The catalog gates models by Codex wire-protocol capability, not Kerkov's
# package version. Keep this pinned to the response/tool contract implemented
# by this provider; advancing it can expose models that need newer semantics.
CODEX_MODELS_CLIENT_VERSION = "0.144.0"


class CodexModelNotAvailableError(ValueError):
    """Raised when a model cannot be used with the authenticated ChatGPT account."""

    def __init__(
        self,
        model: str,
        available_models: Optional[List[str]] = None,
    ) -> None:
        self.model = model
        self.available_models = available_models or []
        message = (
            f"Codex model '{model}' is not available for this ChatGPT account. "
            "Run /models to see available models, then switch with /model <name>."
        )
        if self.available_models:
            message += f" Available: {', '.join(self.available_models)}."
        super().__init__(message)


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
        self._temperature_warned = False
        self._available_models: Optional[List[str]] = None
        self.model_selection_notice: Optional[str] = None
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
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[List["BaseTool"]] = None,
        on_delta: Optional[Callable[[str], None]] = None,
    ) -> LLMResponse:
        if temperature is not None and not self._temperature_warned:
            self.logger.info(
                "Codex Responses ignores the temperature parameter "
                "(sampling is fixed server-side); value will not be forwarded."
            )
            self._temperature_warned = True

        # A shared manager may be refreshing in a model-catalog worker. Both
        # waiting for its lock and the refresh HTTP request must stay off-loop
        # so approvals and cancellation remain responsive.
        await asyncio.to_thread(self._ensure_auth)

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
        # Function-call arguments stream as their own delta events keyed by the
        # item id; a terminal item can echo them empty, so accumulate here.
        argument_buffers: Dict[str, str] = {}
        completed_response: Optional[Dict[str, Any]] = None

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            has_retried_after_refresh = False
            while True:
                headers = self._build_headers()
                async with client.stream(
                    "POST", url, headers=headers, json=payload
                ) as response:
                    if (
                        response.status_code == 401
                        and self.token_manager
                        and not has_retried_after_refresh
                    ):
                        await response.aread()
                        await asyncio.to_thread(self._ensure_auth, force_refresh=True)
                        has_retried_after_refresh = True
                        continue
                    if response.status_code >= 400:
                        response_body = await response.aread()
                        detail = self._backend_error_detail(response_body)
                        if (
                            response.status_code == 400
                            and "model is not supported when using codex with a chatgpt account"
                            in detail.lower()
                        ):
                            raise CodexModelNotAvailableError(
                                self.model,
                                self._available_models,
                            )
                        raise ValueError(
                            f"Codex backend error {response.status_code}: {detail}"
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
                                if on_delta is not None:
                                    on_delta(str(delta))
                        elif event_type == "response.function_call_arguments.delta":
                            item_id = event.get("item_id")
                            delta = event.get("delta")
                            if item_id and isinstance(delta, str):
                                argument_buffers[item_id] = (
                                    argument_buffers.get(item_id, "") + delta
                                )
                        elif event_type == "response.function_call_arguments.done":
                            item_id = event.get("item_id")
                            final_args = event.get("arguments")
                            if item_id and isinstance(final_args, str) and final_args:
                                argument_buffers[item_id] = final_args
                        elif event_type == "response.completed":
                            response_obj = event.get("response")
                            if isinstance(response_obj, dict):
                                completed_response = response_obj
                            self._collect_tool_calls_from_event(
                                event,
                                tool_calls,
                                tool_call_index_by_id,
                                raw_output_items_by_id,
                                argument_buffers,
                            )
                        elif event_type in (
                            "response.output_item.added",
                            "response.output_item.done",
                        ):
                            self._collect_tool_calls_from_event(
                                event,
                                tool_calls,
                                tool_call_index_by_id,
                                raw_output_items_by_id,
                                argument_buffers,
                            )
                    break

        content = "".join(content_parts).strip()
        usage = self._extract_usage(completed_response)
        raw_response = (
            completed_response.copy() if isinstance(completed_response, dict) else {}
        )
        if raw_output_items_by_id:
            raw_response["output"] = list(raw_output_items_by_id.values())
        return LLMResponse(
            content=content,
            model=self.model,
            usage=usage,
            tool_calls=tool_calls if tool_calls else None,
            raw_response=raw_response or completed_response,
        )

    async def get_max_context_tokens(self) -> Optional[int]:
        return None

    async def get_max_output_tokens(self) -> Optional[int]:
        return None

    def validate_connection(self) -> bool:
        try:
            models = self.list_models()
        except Exception as exc:
            self.logger.warning("Could not validate Codex model access: %s", exc)
            return False

        if not models:
            self.logger.warning(
                "Codex authentication succeeded, but no selectable models were returned."
            )
            return False

        self.model_selection_notice = None
        if self.model not in models:
            requested_model = self.model
            super().set_model(models[0])
            self.model_selection_notice = (
                f"Configured Codex model '{requested_model}' is unavailable for "
                f"this ChatGPT account; using '{self.model}'."
            )
        return True

    def list_models(self) -> List[str]:
        self._ensure_auth()
        url = self._resolve_models_url()
        has_retried_after_refresh = False

        with httpx.Client(timeout=self.timeout_seconds) as client:
            while True:
                headers = self._build_headers()
                headers["accept"] = "application/json"
                response = client.get(
                    url,
                    params={"client_version": CODEX_MODELS_CLIENT_VERSION},
                    headers=headers,
                )
                if (
                    response.status_code == 401
                    and self.token_manager
                    and not has_retried_after_refresh
                ):
                    self._ensure_auth(force_refresh=True)
                    has_retried_after_refresh = True
                    continue
                if response.status_code >= 400:
                    raise ValueError(
                        f"Codex backend error {response.status_code}: "
                        f"{self._backend_error_detail(response.content)}"
                    )
                break

        payload = response.json()
        raw_models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(raw_models, list):
            raise ValueError("Codex models response did not contain a model list.")

        models: List[str] = []
        for item in raw_models:
            if not isinstance(item, dict):
                continue
            if str(item.get("visibility", "list")).lower() != "list":
                continue
            slug = item.get("slug") or item.get("id")
            if isinstance(slug, str) and slug and slug not in models:
                models.append(slug)

        self._available_models = models
        return models

    def set_model(self, model: str):
        if self._available_models is not None and model not in self._available_models:
            raise CodexModelNotAvailableError(model, self._available_models)
        super().set_model(model)

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

    def _resolve_models_url(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/codex/responses"):
            base = base[: -len("/responses")]
        elif not base.endswith("/codex"):
            base = f"{base}/codex"
        return f"{base}/models"

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
        system_messages = [
            m.content for m in messages if m.role == "system" and m.content
        ]
        return "\n\n".join(system_messages).strip()

    def _convert_messages_to_responses_input(
        self, messages: List[Message]
    ) -> List[Dict[str, Any]]:
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
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": f"Tool result:\n{message.content}",
                                }
                            ],
                        }
                    )
                continue
            if message.role in ("user", "assistant"):
                if (
                    isinstance(message.raw_blocks, list)
                    and message.raw_blocks
                    and all(
                        self._is_replayable_output_item(block)
                        for block in message.raw_blocks
                    )
                ):
                    items.extend(message.raw_blocks)
                    continue

                calls = (
                    replay_tool_calls(message.raw_blocks)
                    if message.role == "assistant"
                    else []
                )
                if calls:
                    if message.content:
                        items.append(
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [
                                    {"type": "output_text", "text": message.content}
                                ],
                            }
                        )
                    items.extend(
                        {
                            "type": "function_call",
                            "call_id": call.call_id,
                            "name": call.name,
                            "arguments": call.arguments_json(),
                        }
                        for call in calls
                    )
                    continue

                items.append(
                    {
                        "type": "message",
                        "role": message.role,
                        "content": [
                            {
                                "type": (
                                    "input_text"
                                    if message.role == "user"
                                    else "output_text"
                                ),
                                "text": message.content,
                            }
                        ],
                    }
                )
        return items

    @staticmethod
    def _is_replayable_output_item(block: Any) -> bool:
        """Recognize this provider's output before replaying shared raw history."""
        if not isinstance(block, dict):
            return False
        item_type = block.get("type")
        if item_type == "reasoning":
            return True
        if item_type == "message":
            return block.get("role") == "assistant" and isinstance(
                block.get("content"), list
            )
        if item_type == "function_call":
            # Gemini also uses this type name, but stores args rather than
            # the Responses API's serialized arguments.
            return isinstance(block.get("name"), str) and isinstance(
                block.get("arguments"), str
            )
        return False

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
        argument_buffers: Optional[Dict[str, str]] = None,
    ) -> None:
        buffers = argument_buffers or {}
        for item in self._iter_function_call_items(event):
            # Parse from the resolved item so execution and next-turn replay
            # can never disagree about which arguments the model sent.
            item = self._item_with_resolved_arguments(item, buffers)
            tool_call = self._tool_call_from_item(item)
            if tool_call is None:
                continue

            dedupe_key = (
                tool_call.call_id
                or f"{tool_call.tool_name}:{json.dumps(tool_call.parameters, sort_keys=True)}"
            )
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
                raw_output_items_by_id[dedupe_key] = item
            elif not self._item_has_arguments(
                existing_item
            ) and self._item_has_arguments(item):
                raw_output_items_by_id[dedupe_key] = item

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

        return ToolCall(
            tool_name=name,
            parameters=self._parse_arguments(item.get("arguments")),
            call_id=item.get("call_id") or item.get("id"),
        )

    def _parse_arguments(self, raw_args: Any) -> Dict[str, Any]:
        if isinstance(raw_args, dict):
            return raw_args
        if isinstance(raw_args, str) and raw_args.strip():
            try:
                parsed = json.loads(raw_args)
            except json.JSONDecodeError:
                return {}
            if isinstance(parsed, dict):
                return parsed
        return {}

    def _item_with_resolved_arguments(
        self, item: Dict[str, Any], argument_buffers: Dict[str, str]
    ) -> Dict[str, Any]:
        """Copy an item, filling empty or unparseable arguments from the delta buffer."""
        stored = dict(item)
        if self._item_has_arguments(stored):
            return stored
        buffered = argument_buffers.get(stored.get("id", ""))
        if isinstance(buffered, str) and self._parse_arguments(buffered):
            stored["arguments"] = buffered
        return stored

    def _item_has_arguments(self, item: Dict[str, Any]) -> bool:
        raw_args = item.get("arguments")
        if isinstance(raw_args, dict):
            return bool(raw_args)
        if isinstance(raw_args, str):
            return bool(self._parse_arguments(raw_args))
        return False

    def _extract_usage(
        self, response_obj: Optional[Dict[str, Any]]
    ) -> Optional[TokenUsage]:
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

    @staticmethod
    def _backend_error_detail(body: bytes) -> str:
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            return body.decode("utf-8", errors="replace")

        if isinstance(payload, dict):
            detail = payload.get("detail")
            if isinstance(detail, str):
                return detail
            error = payload.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                return error["message"]
        return str(payload)

    def _ensure_auth(self, force_refresh: bool = False) -> None:
        if self.token_manager:
            self.access_token = self.token_manager.ensure_valid_token(
                force_refresh=force_refresh
            )
            self.account_id = self.token_manager.account_id
            self.api_key = self.access_token
            return
        if not self.access_token:
            raise ValueError(
                "Missing Codex access token. Select Codex with /provider to sign in."
            )
        if not self.account_id:
            self.account_id = extract_account_id_from_jwt(self.access_token)
        if not self.account_id:
            raise ValueError(
                "Missing ChatGPT account id in token. Select Codex with /provider "
                "to sign in again."
            )
        self.api_key = self.access_token
