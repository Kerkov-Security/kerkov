from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from kerkov.cli.messages.agents_message import AgentsMessage, BackMessage
from kerkov.cli.messages.approval_info_message import ApprovalInfoMessage
from kerkov.cli.messages.autonomy_info_message import AutonomyInfoMessage
from kerkov.cli.messages.clear_message import ClearMessage
from kerkov.cli.messages.credentials_message import CredentialsMessage
from kerkov.cli.messages.engagements_message import EngagementsMessage
from kerkov.cli.messages.general_message import GeneralMessage
from kerkov.cli.messages.help_message import HelpMessage
from kerkov.cli.messages.logout_message import LogoutMessage
from kerkov.cli.messages.mode_info_message import ModeInfoMessge
from kerkov.cli.messages.model_info_message import ModelInfoMessage
from kerkov.cli.messages.prompt_message import PromptMessage
from kerkov.cli.messages.provider_info_message import ProviderInfoMessage
from kerkov.cli.messages.quit_message import QuitMessage
from kerkov.cli.messages.resource_message import ResourceMessage
from kerkov.cli.messages.resume_message import ResumeMessage
from kerkov.cli.messages.searxng_message import SearxngMessage
from kerkov.cli.messages.skill_message import (
    SkillListMessage,
    SkillLoadMessage,
    SkillSearchMessage,
)
from kerkov.cli.messages.switch_approval_message import SwitchApprovalMessage
from kerkov.cli.messages.switch_autonomy_message import SwitchAutonomyMessage
from kerkov.cli.messages.switch_mode_message import SwitchModeMessage
from kerkov.cli.messages.switch_model_message import SwitchModelMessage
from kerkov.cli.messages.switch_provider_message import SwitchProviderMessage
from kerkov.cli.messages.switch_verbosity_message import SwitchVerbosityMessage
from kerkov.cli.messages.usage_message import UsageMessage
from kerkov.cli.messages.user_message import UserMessage
from kerkov.cli.messages.verbosity_info_message import VerbosityInfoMessage
from kerkov.cli.messages.yolo_message import YoloMessage
from kerkov.cli.messages.zap_message import ZapMessage
from kerkov.core.commands import (
    COMMAND_SPECS,
    CommandSpec,
    HelpEntry,
    Intent,
    find_spec,
    parse,
)
from kerkov.core.knowledge_base.models import LedgerSection

# A command builder turns the parsed tokens (and the raw input, for commands
# like /prompt that care about quoting/whitespace) into a UserMessage.
CommandBuilder = Callable[[List[str], str], UserMessage]


@dataclass(frozen=True)
class Command:
    """A shared `CommandSpec` paired with the CLI handler that builds its message.

    The spec (names, intent, docs) lives in `core.commands` so the terminal and
    the web console read one source; `.names`/`.help` delegate to it, keeping the
    parser, `/help`, and the web catalog from drifting apart.
    """

    spec: CommandSpec
    build: CommandBuilder

    @property
    def names(self) -> List[str]:
        return self.spec.names

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def aliases(self) -> List[str]:
        return self.spec.aliases

    @property
    def help(self) -> HelpEntry:
        return self.spec.help


class InputParser:

    @staticmethod
    def parse_for_message_type(user_input: str) -> UserMessage:
        parsed = parse(user_input)
        builder = _BUILDERS.get(parsed.intent)
        if builder is None:
            return GeneralMessage(parsed.raw)
        return builder(parsed.parts, parsed.raw)


def _build_mode(parts: List[str], raw_input: str) -> UserMessage:
    lowered = [p.lower() for p in parts]
    if len(lowered) == 1:
        return ModeInfoMessge()
    return SwitchModeMessage(lowered[1])


def _build_model(parts: List[str], raw_input: str) -> UserMessage:
    if len(parts) == 1:
        return ModelInfoMessage()
    return SwitchModelMessage(" ".join(parts[1:]).strip())


def _build_provider(parts: List[str], raw_input: str) -> UserMessage:
    if len(parts) == 1:
        return ProviderInfoMessage()
    return SwitchProviderMessage(" ".join(parts[1:]).strip())


def _build_autonomy(parts: List[str], raw_input: str) -> UserMessage:
    if len(parts) == 1:
        return AutonomyInfoMessage()
    return SwitchAutonomyMessage(parts[1].strip())


def _build_verbosity(parts: List[str], raw_input: str) -> UserMessage:
    if len(parts) == 1:
        return VerbosityInfoMessage()
    return SwitchVerbosityMessage(parts[1].strip())


def _build_approval(parts: List[str], raw_input: str) -> UserMessage:
    if len(parts) == 1:
        return ApprovalInfoMessage()
    return SwitchApprovalMessage(parts[1].strip())


def _build_resume(parts: List[str], raw_input: str) -> UserMessage:
    if len(parts) == 1:
        return ResumeMessage()
    return ResumeMessage(parts[1].strip())


def _build_usage(parts: List[str], raw_input: str) -> UserMessage:
    if len(parts) > 2:
        return UsageMessage(invalid=True)
    return UsageMessage(parts[1] if len(parts) == 2 else None)


def _build_skill(parts: List[str], raw_input: str) -> UserMessage:
    if len(parts) == 1:
        return SkillListMessage()
    if parts[1].lower() == "search" and len(parts) > 2:
        return SkillSearchMessage(" ".join(parts[2:]).strip())
    skill_id = parts[1].strip()
    skill_args = " ".join(parts[2:]).strip() if len(parts) > 2 else None
    return SkillLoadMessage(skill_id, skill_args)


def _build_yolo(parts: List[str], raw_input: str) -> UserMessage:
    return YoloMessage(parts[1] if len(parts) > 1 else None)


def _build_prompt(parts: List[str], raw_input: str) -> UserMessage:
    remainder = raw_input[len("/prompt") :].strip()
    if not remainder:
        return PromptMessage()
    if (remainder.startswith('"') and remainder.endswith('"')) or (
        remainder.startswith("'") and remainder.endswith("'")
    ):
        remainder = remainder[1:-1]
    return PromptMessage(path=remainder)


def _build_help(parts: List[str], raw_input: str) -> UserMessage:
    if len(parts) == 1:
        return HelpMessage()
    return HelpMessage(parts[1].strip())


def _build_auth(parts: List[str], raw_input: str) -> UserMessage:
    if len(parts) == 1:
        return CredentialsMessage()

    action = parts[1].strip().lower()
    if action in {"set", "add"}:
        if len(parts) not in {3, 4}:
            return CredentialsMessage(action="invalid")
        session_only = len(parts) == 4 and parts[3].lower() == "--session"
        if len(parts) == 4 and not session_only:
            return CredentialsMessage(action="invalid")
        return CredentialsMessage(
            action="set",
            target=parts[2].strip(),
            session_only=session_only,
        )

    if action in {"remove", "delete", "forget"}:
        if len(parts) != 3:
            return CredentialsMessage(action="invalid")
        return CredentialsMessage(action="remove", target=parts[2].strip())

    # Convenient shorthand: `/auth tavily` opens the hidden Tavily prompt.
    if len(parts) == 2:
        return CredentialsMessage(action="set", target=parts[1].strip())
    return CredentialsMessage(action="invalid")


def _build_zap(parts: List[str], raw_input: str) -> UserMessage:
    if len(parts) == 1:
        return ZapMessage(action="status")
    if len(parts) == 2 and parts[1].strip().lower() in {"status", "stop"}:
        return ZapMessage(action=parts[1].strip().lower())
    return ZapMessage(action="invalid")


def _build_searxng(parts: List[str], raw_input: str) -> UserMessage:
    if len(parts) == 1:
        return SearxngMessage(action="status")
    action = parts[1].strip().lower()
    if len(parts) == 2 and action in {"setup", "start", "stop", "status"}:
        return SearxngMessage(action=action)
    return SearxngMessage(action="invalid")


def _build_engagements(parts: List[str], raw_input: str) -> UserMessage:
    return EngagementsMessage(
        show_agents=len(parts) > 1 and parts[1].strip().lower() == "agents"
    )


def _build_agents(parts: List[str], raw_input: str) -> UserMessage:
    if len(parts) == 1:
        return AgentsMessage()
    verb = parts[1].strip().lower()
    if verb == "stop":
        # No bare `/agents stop` shorthand for "stop everything": mass cancel
        # already has a name (`/q`) and an accidental one is unrecoverable.
        if len(parts) != 3:
            return AgentsMessage(action="invalid")
        return AgentsMessage(parts[2].strip(), action="stop")
    if verb == "review":
        if len(parts) == 3:
            return AgentsMessage(parts[2].strip(), action="review")
        if len(parts) == 4 and parts[3].strip().lower() in ("on", "off"):
            return AgentsMessage(
                parts[2].strip(),
                action="review",
                value=parts[3].strip().lower() == "on",
            )
        return AgentsMessage(action="invalid")
    return AgentsMessage(parts[1].strip())


def _build_logout(parts: List[str], raw_input: str) -> UserMessage:
    if len(parts) == 1:
        return LogoutMessage()
    return LogoutMessage(" ".join(parts[1:]).strip())


_BUILDERS: Dict[Intent, CommandBuilder] = {
    Intent.HELP: _build_help,
    Intent.PROMPT: _build_prompt,
    Intent.CLEAR: lambda parts, raw_input: ClearMessage(),
    Intent.QUIT: lambda parts, raw_input: QuitMessage(),
    Intent.MODE: _build_mode,
    Intent.AUTONOMY: _build_autonomy,
    Intent.VERBOSITY: _build_verbosity,
    Intent.APPROVAL: _build_approval,
    Intent.YOLO: _build_yolo,
    Intent.PROVIDER: _build_provider,
    Intent.MODEL: _build_model,
    Intent.SKILL: _build_skill,
    Intent.ASSET: lambda parts, raw_input: ResourceMessage(
        LedgerSection.ASSETS, raw_input
    ),
    Intent.TARGET: lambda parts, raw_input: ResourceMessage(
        LedgerSection.TARGETS, raw_input
    ),
    Intent.AGENTS: _build_agents,
    Intent.BACK: lambda parts, raw_input: BackMessage(),
    Intent.ENGAGEMENTS: _build_engagements,
    Intent.USAGE: _build_usage,
    Intent.RESUME: _build_resume,
    Intent.ZAP: _build_zap,
    Intent.SEARXNG: _build_searxng,
    Intent.AUTH: _build_auth,
    Intent.LOGOUT: _build_logout,
}


# Pairs each shared spec with its CLI handler. `/help` renders from `.help` and
# `parse_for_message_type` dispatches through `_BUILDERS`, so the terminal and
# web console stay in step with `core.commands`.
COMMANDS: List[Command] = [
    Command(spec=spec, build=_BUILDERS[spec.intent]) for spec in COMMAND_SPECS
]


def find_command(token: str) -> Optional[Command]:
    """Look up a command by any of its names (slash optional, case-insensitive)."""
    spec = find_spec(token)
    if spec is None:
        return None
    return next(command for command in COMMANDS if command.spec is spec)
