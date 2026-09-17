"""Tests for AnthropicProvider prompt caching, breakpoints, and usage summing."""

import json
import types
import unittest

from kerkov.models.basic_models import Message
from kerkov.providers.anthrophic_provider import AnthropicProvider
from kerkov.tools.exec_tool import ExecTool
from kerkov.tools.http_tool import HTTPTool


def _make_provider() -> AnthropicProvider:
    return AnthropicProvider(api_key="test-key", model="claude-test")


def _tools():
    return [ExecTool(), HTTPTool()]


CACHE = {"type": "ephemeral"}


def _cache_control(block):
    return block.get("cache_control") if isinstance(block, dict) else None


class SystemBreakpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = _make_provider()

    def test_system_prelude_carries_ephemeral_breakpoint(self):
        request = self.provider._build_request(
            [
                Message(role="system", content="STABLE SYSTEM PROMPT"),
                Message(role="user", content="hello"),
            ],
            None,
            None,
            _tools(),
        )
        system = request["system"]
        self.assertIsInstance(system, list)
        self.assertEqual(system[-1]["text"], "STABLE SYSTEM PROMPT")
        self.assertEqual(system[-1]["cache_control"], CACHE)
        # With a real system prompt the tools are cached behind it, not marked.
        self.assertNotIn("cache_control", request["tools"][-1])

    def test_empty_system_falls_back_to_last_tool_breakpoint(self):
        request = self.provider._build_request(
            [Message(role="user", content="hello")],
            None,
            None,
            _tools(),
        )
        self.assertEqual(request["system"], "")
        self.assertEqual(request["tools"][-1]["cache_control"], CACHE)
        self.assertNotIn("cache_control", request["tools"][0])

    def test_empty_system_and_no_tools_has_no_prefix_breakpoint(self):
        request = self.provider._build_request(
            [Message(role="user", content="hello")],
            None,
            None,
            None,
        )
        self.assertEqual(request["system"], "")
        self.assertEqual(request["tools"], [])


class MessageBreakpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = _make_provider()

    def test_last_turn_block_carries_ephemeral_breakpoint(self):
        request = self.provider._build_request(
            [
                Message(role="system", content="sys"),
                Message(role="user", content="first"),
                Message(role="assistant", content="answer"),
                Message(role="user", content="second"),
            ],
            None,
            None,
            None,
        )
        last = request["messages"][-1]
        self.assertEqual(last["role"], "user")
        self.assertEqual(_cache_control(last["content"][-1]), CACHE)

    def test_no_breakpoint_when_no_messages(self):
        request = self.provider._build_request(
            [Message(role="system", content="sys")],
            None,
            None,
            None,
        )
        self.assertEqual(request["messages"], [])


class LedgerTailTest(unittest.TestCase):
    """The mutable ledger tail must ride after the last cache breakpoint."""

    def setUp(self) -> None:
        self.provider = _make_provider()

    def _request_with_ledger(self):
        return self.provider._build_request(
            [
                Message(role="system", content="sys"),
                Message(role="user", content="do the thing"),
                Message(role="system", content="LEDGER: flag=HTB{x}"),
            ],
            None,
            None,
            _tools(),
        )

    def test_ledger_is_last_block_and_uncached(self):
        request = self._request_with_ledger()
        blocks = request["messages"][-1]["content"]
        self.assertEqual(blocks[-1]["text"], "LEDGER: flag=HTB{x}")
        self.assertIsNone(_cache_control(blocks[-1]))

    def test_breakpoint_sits_before_the_ledger_tail(self):
        request = self._request_with_ledger()
        blocks = request["messages"][-1]["content"]
        # The conversation block ("do the thing") is cached; the ledger after it
        # is not, so a ledger change never invalidates the cached prefix.
        self.assertEqual(blocks[0]["text"], "do the thing")
        self.assertEqual(_cache_control(blocks[0]), CACHE)

    def test_ledger_does_not_create_a_consecutive_user_turn(self):
        request = self._request_with_ledger()
        # Only one user message: the ledger merged into the trailing turn.
        self.assertEqual(len(request["messages"]), 1)

    def test_multiple_trailing_system_messages_all_ride_the_tail(self):
        request = self.provider._build_request(
            [
                Message(role="system", content="sys"),
                Message(role="user", content="q"),
                Message(role="system", content="NUDGE"),
                Message(role="system", content="LEDGER"),
            ],
            None,
            None,
            None,
        )
        tail_text = request["messages"][-1]["content"][-1]["text"]
        self.assertIn("NUDGE", tail_text)
        self.assertIn("LEDGER", tail_text)


class ByteStabilityTest(unittest.TestCase):
    """Equivalent turns must produce byte-identical tool/system prefixes."""

    def setUp(self) -> None:
        self.provider = _make_provider()

    def test_tools_and_system_are_byte_stable_across_builds(self):
        messages = [
            Message(role="system", content="STABLE SYSTEM PROMPT"),
            Message(role="user", content="hello"),
        ]
        first = self.provider._build_request(messages, None, None, _tools())
        second = self.provider._build_request(messages, None, None, _tools())
        self.assertEqual(
            json.dumps(first["tools"], sort_keys=False),
            json.dumps(second["tools"], sort_keys=False),
        )
        self.assertEqual(
            json.dumps(first["system"], sort_keys=False),
            json.dumps(second["system"], sort_keys=False),
        )

    def test_prefix_stable_when_history_and_ledger_grow(self):
        base = [
            Message(role="system", content="STABLE SYSTEM PROMPT"),
            Message(role="user", content="hello"),
            Message(role="system", content="LEDGER v1"),
        ]
        grown = [
            Message(role="system", content="STABLE SYSTEM PROMPT"),
            Message(role="user", content="hello"),
            Message(role="assistant", content="hi there"),
            Message(role="user", content="next"),
            Message(role="system", content="LEDGER v2 changed"),
        ]
        turn1 = self.provider._build_request(base, None, None, _tools())
        turn2 = self.provider._build_request(grown, None, None, _tools())
        # System + tools prefix is identical despite the ledger changing.
        self.assertEqual(turn1["system"], turn2["system"])
        self.assertEqual(turn1["tools"], turn2["tools"])


class UsageSummingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = _make_provider()

    def _message(self, **usage):
        return types.SimpleNamespace(usage=types.SimpleNamespace(**usage))

    def test_prompt_tokens_sum_cached_and_uncached(self):
        msg = self._message(
            input_tokens=100,
            output_tokens=30,
            cache_read_input_tokens=900,
            cache_creation_input_tokens=50,
        )
        usage = self.provider._usage_from_message(msg)
        self.assertEqual(usage.prompt_tokens, 1050)
        self.assertEqual(usage.completion_tokens, 30)
        self.assertEqual(usage.total_tokens, 1080)
        self.assertEqual(usage.cache_read_tokens, 900)
        self.assertEqual(usage.cache_creation_tokens, 50)

    def test_missing_cache_fields_default_to_zero(self):
        msg = self._message(input_tokens=200, output_tokens=10)
        usage = self.provider._usage_from_message(msg)
        self.assertEqual(usage.prompt_tokens, 200)
        self.assertEqual(usage.total_tokens, 210)
        self.assertEqual(usage.cache_read_tokens, 0)
        self.assertEqual(usage.cache_creation_tokens, 0)

    def test_no_usage_returns_none(self):
        self.assertIsNone(
            self.provider._usage_from_message(types.SimpleNamespace(usage=None))
        )


class CachesPrefixFlagTest(unittest.TestCase):
    def test_anthropic_advertises_prefix_caching(self):
        self.assertTrue(_make_provider().caches_prompt_prefix)


if __name__ == "__main__":
    unittest.main()
