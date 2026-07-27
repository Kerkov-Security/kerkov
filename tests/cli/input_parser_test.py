"""Tests for InputParser dispatching to the verbosity/approval message classes."""

import unittest

from kerkov.cli.input_parser import InputParser
from kerkov.cli.messages.approval_info_message import ApprovalInfoMessage
from kerkov.cli.messages.autonomy_info_message import AutonomyInfoMessage
from kerkov.cli.messages.credentials_message import CredentialsMessage
from kerkov.cli.messages.model_info_message import ModelInfoMessage
from kerkov.cli.messages.provider_info_message import ProviderInfoMessage
from kerkov.cli.messages.switch_approval_message import SwitchApprovalMessage
from kerkov.cli.messages.switch_autonomy_message import SwitchAutonomyMessage
from kerkov.cli.messages.switch_model_message import SwitchModelMessage
from kerkov.cli.messages.switch_provider_message import SwitchProviderMessage
from kerkov.cli.messages.switch_verbosity_message import SwitchVerbosityMessage
from kerkov.cli.messages.verbosity_info_message import VerbosityInfoMessage
from kerkov.cli.messages.yolo_message import YoloMessage


class InputParserVerbosityTest(unittest.TestCase):

    def test_bare_verbosity_returns_info(self):
        result = InputParser.parse_for_message_type("/verbosity")
        self.assertIsInstance(result, VerbosityInfoMessage)

    def test_verbosity_with_arg_returns_switch(self):
        result = InputParser.parse_for_message_type("/verbosity silent")
        self.assertIsInstance(result, SwitchVerbosityMessage)
        self.assertEqual(result.verbosity_str, "silent")


class InputParserApprovalTest(unittest.TestCase):

    def test_bare_approval_returns_info(self):
        result = InputParser.parse_for_message_type("/approval")
        self.assertIsInstance(result, ApprovalInfoMessage)

    def test_approval_with_arg_returns_switch(self):
        result = InputParser.parse_for_message_type("/approval permissive")
        self.assertIsInstance(result, SwitchApprovalMessage)
        self.assertEqual(result.mode_str, "permissive")


class InputParserAutonomyTest(unittest.TestCase):

    def test_bare_autonomy_returns_info(self):
        result = InputParser.parse_for_message_type("/autonomy")
        self.assertIsInstance(result, AutonomyInfoMessage)

    def test_autonomy_with_arg_returns_switch(self):
        result = InputParser.parse_for_message_type("/autonomy 3")
        self.assertIsInstance(result, SwitchAutonomyMessage)


class InputParserYoloTest(unittest.TestCase):

    def test_bare_yolo(self):
        result = InputParser.parse_for_message_type("/yolo")
        self.assertIsInstance(result, YoloMessage)
        self.assertEqual(result.arg, "")

    def test_yolo_on(self):
        result = InputParser.parse_for_message_type("/yolo on")
        self.assertIsInstance(result, YoloMessage)
        self.assertEqual(result.arg, "on")


class InputParserProviderTest(unittest.TestCase):

    def test_bare_provider_opens_provider_picker(self):
        result = InputParser.parse_for_message_type("/provider")
        self.assertIsInstance(result, ProviderInfoMessage)

    def test_plural_provider_alias_opens_same_picker(self):
        result = InputParser.parse_for_message_type("/providers")
        self.assertIsInstance(result, ProviderInfoMessage)

    def test_plural_alias_can_switch_directly(self):
        result = InputParser.parse_for_message_type("/providers ollama")
        self.assertIsInstance(result, SwitchProviderMessage)
        self.assertEqual(result.new_provider_name, "ollama")


class InputParserModelTest(unittest.TestCase):

    def test_bare_model_opens_model_picker(self):
        result = InputParser.parse_for_message_type("/model")
        self.assertIsInstance(result, ModelInfoMessage)

    def test_plural_model_alias_opens_same_picker(self):
        result = InputParser.parse_for_message_type("/models")
        self.assertIsInstance(result, ModelInfoMessage)

    def test_plural_alias_can_switch_directly(self):
        result = InputParser.parse_for_message_type("/models gpt-5.6-terra")
        self.assertIsInstance(result, SwitchModelMessage)
        self.assertEqual(result.model_name, "gpt-5.6-terra")


class InputParserCredentialsTest(unittest.TestCase):

    def test_bare_auth_returns_status(self):
        result = InputParser.parse_for_message_type("/auth")
        self.assertIsInstance(result, CredentialsMessage)
        self.assertEqual(result.action, "status")

    def test_auth_set_uses_separate_secret_prompt(self):
        result = InputParser.parse_for_message_type("/auth set gemini")
        self.assertIsInstance(result, CredentialsMessage)
        self.assertEqual(result.action, "set")
        self.assertEqual(result.target, "gemini")
        self.assertFalse(result.session_only)

    def test_auth_session_flag(self):
        result = InputParser.parse_for_message_type("/auth set openrouter --session")
        self.assertTrue(result.session_only)

    def test_auth_shorthand_targets_tavily(self):
        result = InputParser.parse_for_message_type("/auth tavily")
        self.assertEqual(result.action, "set")
        self.assertEqual(result.target, "tavily")

    def test_auth_rejects_secret_in_command_position(self):
        result = InputParser.parse_for_message_type(
            "/auth set gemini should-not-be-accepted"
        )
        self.assertEqual(result.action, "invalid")


if __name__ == "__main__":
    unittest.main()
