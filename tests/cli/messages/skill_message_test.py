"""Tests for skill slash command message types."""

import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from kerkov.cli.messages.skill_message import (
    SkillListMessage,
    SkillLoadMessage,
    SkillSearchMessage,
)
from kerkov.context.skills.skill_manager import SkillManager
from kerkov.core.credentials import (
    CredentialInput,
    CredentialManager,
    MemorySecretStore,
)

VALID_SKILL_MD = """\
---
name: test-skill
description: "A test skill"
user-invocable: true
metadata:
  kerkov:
    category: recon/local
    difficulty: beginner
    tags: [recon, test]
    requires:
      bins: [nmap]
---

# Test Skill

Instructions here.
"""

NOT_USER_INVOCABLE_MD = """\
---
name: internal-skill
description: "Internal only"
user-invocable: false
---

Internal content.
"""

AUTH_SKILL_MD = """\
---
name: auth-skill
description: "A skill requiring an API token"
user-invocable: true
metadata:
  kerkov:
    requires:
      credentials: [HTB_API_TOKEN]
---

Authenticated instructions.
"""


def _create_skill_dir(base_dir: str, dir_name: str, content: str) -> None:
    skill_dir = os.path.join(base_dir, dir_name)
    os.makedirs(skill_dir, exist_ok=True)
    with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(content)


def _create_mock_cli(skill_manager: SkillManager) -> MagicMock:
    cli = MagicMock()
    cli.console = MagicMock()
    cli.skill_manager = skill_manager
    cli.credentials = CredentialManager(
        environment={},
        persistent_store=MemorySecretStore(),
    )
    cli.prompt_for_credential = AsyncMock()
    cli.get_response = AsyncMock()
    return cli


# ---------------------------------------------------------------------------
# SkillListMessage
# ---------------------------------------------------------------------------
class SkillListMessageTest(unittest.TestCase):

    def _run(self, coro):
        return asyncio.run(coro)

    def test_list_no_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SkillManager(skills_dir=tmp)
            cli = _create_mock_cli(mgr)

            msg = SkillListMessage()
            self._run(msg.execute(cli))

            cli.console.print.assert_any_call("[dim]No skills loaded.[/dim]")

    @patch(
        "kerkov.context.skills.skill_manager.shutil.which", return_value="/usr/bin/nmap"
    )
    def test_list_with_skills(self, mock_which):
        with tempfile.TemporaryDirectory() as tmp:
            _create_skill_dir(tmp, "test-skill", VALID_SKILL_MD)
            mgr = SkillManager(skills_dir=tmp)
            cli = _create_mock_cli(mgr)

            msg = SkillListMessage()
            self._run(msg.execute(cli))

            # Should have printed a table (Rich Table is passed to console.print)
            # Verify print was called multiple times (table, hints, spacing)
            assert cli.console.print.call_count >= 3


# ---------------------------------------------------------------------------
# SkillLoadMessage
# ---------------------------------------------------------------------------
class SkillLoadMessageTest(unittest.TestCase):

    def _run(self, coro):
        return asyncio.run(coro)

    def test_load_existing_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            _create_skill_dir(tmp, "test-skill", VALID_SKILL_MD)
            mgr = SkillManager(skills_dir=tmp)
            cli = _create_mock_cli(mgr)

            msg = SkillLoadMessage("test-skill")
            self._run(msg.execute(cli))

            cli.get_response.assert_called_once()
            prompt = cli.get_response.call_args[0][0]
            assert "test-skill" in prompt
            assert "# Test Skill" in prompt

    def test_load_nonexistent_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SkillManager(skills_dir=tmp)
            cli = _create_mock_cli(mgr)

            msg = SkillLoadMessage("nonexistent")
            self._run(msg.execute(cli))

            cli.get_response.assert_not_called()
            cli.console.print.assert_any_call(
                "[red]Skill 'nonexistent' not found.[/red]"
            )

    def test_load_not_user_invocable(self):
        with tempfile.TemporaryDirectory() as tmp:
            _create_skill_dir(tmp, "internal", NOT_USER_INVOCABLE_MD)
            mgr = SkillManager(skills_dir=tmp)
            cli = _create_mock_cli(mgr)

            msg = SkillLoadMessage("internal-skill")
            self._run(msg.execute(cli))

            cli.get_response.assert_not_called()
            cli.console.print.assert_any_call(
                "[yellow]Skill 'internal-skill' is not user-invocable.[/yellow]"
            )

    def test_load_with_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            _create_skill_dir(tmp, "test-skill", VALID_SKILL_MD)
            mgr = SkillManager(skills_dir=tmp)
            cli = _create_mock_cli(mgr)

            msg = SkillLoadMessage("test-skill", skill_args="target.bin")
            self._run(msg.execute(cli))

            cli.get_response.assert_called_once()
            prompt = cli.get_response.call_args[0][0]
            assert "target.bin" in prompt

    def test_load_prompts_for_declared_credential(self):
        with tempfile.TemporaryDirectory() as tmp:
            _create_skill_dir(tmp, "auth-skill", AUTH_SKILL_MD)
            mgr = SkillManager(skills_dir=tmp)
            cli = _create_mock_cli(mgr)
            cli.prompt_for_credential.return_value = CredentialInput(
                "htb-secret",
                persist=True,
            )

            msg = SkillLoadMessage("auth-skill")
            self._run(msg.execute(cli))

            cli.prompt_for_credential.assert_awaited_once()
            cli.get_response.assert_awaited_once()

    @patch("kerkov.context.skills.skill_manager.shutil.which", return_value=None)
    def test_load_warns_missing_bins(self, mock_which):
        with tempfile.TemporaryDirectory() as tmp:
            _create_skill_dir(tmp, "test-skill", VALID_SKILL_MD)
            mgr = SkillManager(skills_dir=tmp)
            cli = _create_mock_cli(mgr)

            msg = SkillLoadMessage("test-skill")
            self._run(msg.execute(cli))

            # Should still proceed (get_response called) but warn
            cli.get_response.assert_called_once()
            # Check that a warning about missing binaries was printed
            printed_args = [str(call) for call in cli.console.print.call_args_list]
            assert any("Missing required binaries" in s for s in printed_args)


# ---------------------------------------------------------------------------
# SkillSearchMessage
# ---------------------------------------------------------------------------
class SkillSearchMessageTest(unittest.TestCase):

    def _run(self, coro):
        return asyncio.run(coro)

    def test_search_with_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            _create_skill_dir(tmp, "test-skill", VALID_SKILL_MD)
            mgr = SkillManager(skills_dir=tmp)
            cli = _create_mock_cli(mgr)

            msg = SkillSearchMessage("recon")
            self._run(msg.execute(cli))

            printed_args = [str(call) for call in cli.console.print.call_args_list]
            assert any("test-skill" in s for s in printed_args)

    def test_search_no_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SkillManager(skills_dir=tmp)
            cli = _create_mock_cli(mgr)

            msg = SkillSearchMessage("zzzznotfound")
            self._run(msg.execute(cli))

            cli.console.print.assert_any_call(
                "[dim]No skills matched 'zzzznotfound'[/dim]"
            )


if __name__ == "__main__":
    unittest.main()
