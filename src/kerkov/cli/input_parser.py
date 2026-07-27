from dataclasses import dataclass
from typing import Callable, List, Optional

from kerkov.cli.messages.approval_info_message import ApprovalInfoMessage
from kerkov.cli.messages.autonomy_info_message import AutonomyInfoMessage
from kerkov.cli.messages.clear_message import ClearMessage
from kerkov.cli.messages.codex_login_message import CodexLoginMessage
from kerkov.cli.messages.credentials_message import CredentialsMessage
from kerkov.cli.messages.engagements_message import EngagementsMessage
from kerkov.cli.messages.general_message import GeneralMessage
from kerkov.cli.messages.help_entry import HelpEntry
from kerkov.cli.messages.help_message import HelpMessage
from kerkov.cli.messages.mode_info_message import ModeInfoMessge
from kerkov.cli.messages.model_info_message import ModelInfoMessage
from kerkov.cli.messages.prompt_message import PromptMessage
from kerkov.cli.messages.provider_info_message import ProviderInfoMessage
from kerkov.cli.messages.quit_message import QuitMessage
from kerkov.cli.messages.resume_message import ResumeMessage
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
from kerkov.cli.messages.user_message import UserMessage
from kerkov.cli.messages.verbosity_info_message import VerbosityInfoMessage
from kerkov.cli.messages.yolo_message import YoloMessage

# A command builder turns the parsed tokens (and the raw input, for commands
# like /prompt that care about quoting/whitespace) into a UserMessage.
CommandBuilder = Callable[[List[str], str], UserMessage]


@dataclass(frozen=True)
class Command:
    """One slash command: its dispatch names, its docs, and how to build it.

    `names[0]` is canonical; the rest are aliases (e.g. `quit`, `exit` for `/q`).
    `COMMANDS` below is the single source of truth — the parser dispatches from
    it and `/help` renders from it, so the two cannot drift apart.
    """

    names: List[str]
    help: HelpEntry
    build: CommandBuilder

    @property
    def name(self) -> str:
        return self.names[0]

    @property
    def aliases(self) -> List[str]:
        return self.names[1:]


class InputParser:

    @staticmethod
    def parse_for_message_type(user_input: str) -> UserMessage:
        raw_input = user_input.strip()
        if not raw_input:
            return GeneralMessage("")

        parts = raw_input.split()
        token = parts[0].lower()

        command = _COMMAND_BY_NAME.get(token)
        if command is None:
            return GeneralMessage(raw_input)
        return command.build(parts, raw_input)


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


COMMANDS: List[Command] = [
    # --- Session ------------------------------------------------------------
    Command(
        names=["/help"],
        build=_build_help,
        help=HelpEntry(
            category="Session",
            summary="Show this command help; pass a command for detailed docs.",
            syntax=["/help", "/help <command>"],
            details=(
                "With no argument, lists every slash command grouped by category. "
                "Pass a command name (with or without the leading slash) to see its "
                "full syntax, aliases, examples, and related commands."
            ),
            examples=["/help", "/help skill", "/help /mode"],
            related=["/mode", "/skill"],
        ),
    ),
    Command(
        names=["/prompt"],
        build=_build_prompt,
        help=HelpEntry(
            category="Session",
            summary="Compose a message in your editor, or send one loaded from a file.",
            syntax=["/prompt", "/prompt <path>"],
            details=(
                "With no argument, opens your $EDITOR to compose a multi-line "
                "message. With a path, loads the file's contents and sends it as "
                "your next message. Useful for long or pre-written prompts."
            ),
            examples=["/prompt", "/prompt ./notes/recon-brief.md"],
        ),
    ),
    Command(
        names=["/clear"],
        build=lambda parts, raw_input: ClearMessage(),
        help=HelpEntry(
            category="Session",
            summary="Clear conversation history and start a fresh session.",
            syntax=["/clear"],
            details=(
                "Resets the conversation history, token tracker, and session "
                "approval rules. The current mode, autonomy, provider, and model "
                "are kept."
            ),
        ),
    ),
    Command(
        names=["/q", "quit", "exit"],
        build=lambda parts, raw_input: QuitMessage(),
        help=HelpEntry(
            category="Session",
            summary="Exit Kerkov Bot.",
            syntax=["/q"],
            details="Exits the CLI and prints the command to resume this engagement later.",
            related=["/resume"],
        ),
    ),
    # --- Modes & Autonomy ---------------------------------------------------
    Command(
        names=["/mode"],
        build=_build_mode,
        help=HelpEntry(
            category="Modes & Autonomy",
            summary="Show the current operational mode, or switch to another.",
            syntax=["/mode", "/mode [recon|audit|offensive|ctf]"],
            details=(
                "With no argument, shows the current mode and lists all modes. With "
                "a mode name, switches to it. Each mode carries its own system "
                "prompt and posture: recon (passive), audit (default), offensive "
                "(aggressive), and ctf (capture-the-flag)."
            ),
            examples=["/mode", "/mode offensive", "/mode ctf"],
            related=["/autonomy"],
        ),
    ),
    Command(
        names=["/autonomy"],
        build=_build_autonomy,
        help=HelpEntry(
            category="Modes & Autonomy",
            summary="Show or set the autonomy preset (verbosity + approval together).",
            syntax=[
                "/autonomy",
                "/autonomy <1-4|guided|standard|aggressive|autonomous>",
            ],
            details=(
                "With no argument, shows the current preset. Setting a level adjusts "
                "both narration verbosity and approval gating at once. Levels: "
                "1 guided, 2 standard (default), 3 aggressive, 4 autonomous. Level 4 "
                "requires ALLOW_AUTONOMOUS=true."
            ),
            examples=["/autonomy", "/autonomy 3", "/autonomy aggressive"],
            related=["/verbosity", "/approval"],
        ),
    ),
    Command(
        names=["/verbosity"],
        build=_build_verbosity,
        help=HelpEntry(
            category="Modes & Autonomy",
            summary="Change narration verbosity only (not approval gating).",
            syntax=["/verbosity", "/verbosity <guided|standard|aggressive|silent>"],
            details=(
                "Controls how much the assistant narrates its actions, independent "
                "of approval gating. To change both at once, use /autonomy."
            ),
            examples=["/verbosity", "/verbosity silent"],
            related=["/autonomy", "/approval"],
        ),
    ),
    Command(
        names=["/approval"],
        build=_build_approval,
        help=HelpEntry(
            category="Modes & Autonomy",
            summary="Change tool-approval gating only (not narration).",
            syntax=["/approval", "/approval <strict|normal|permissive|yolo>"],
            details=(
                "Controls when tool calls require your approval, independent of "
                "verbosity. 'yolo' bypasses all approvals. To change both at once, "
                "use /autonomy."
            ),
            examples=["/approval", "/approval permissive"],
            related=["/autonomy", "/yolo", "/verbosity"],
        ),
    ),
    Command(
        names=["/yolo"],
        build=_build_yolo,
        help=HelpEntry(
            category="Modes & Autonomy",
            summary="Toggle the approval bypass (sugar for /approval yolo|normal).",
            syntax=["/yolo", "/yolo [on|off]"],
            details=(
                "With no argument, toggles YOLO mode. YOLO bypasses all "
                "tool-approval prompts for the session; turning it off restores "
                "normal gating."
            ),
            examples=["/yolo", "/yolo on", "/yolo off"],
            related=["/approval"],
        ),
    ),
    # --- Providers & Models -------------------------------------------------
    Command(
        names=["/provider", "/providers"],
        build=_build_provider,
        help=HelpEntry(
            category="Providers & Models",
            summary="Choose an LLM provider or switch directly by name.",
            syntax=["/provider", "/provider <name>"],
            details=(
                "With no name, opens an interactive provider picker and highlights "
                "the active provider. `/providers` is a compatibility alias. "
                "Providers: anthropic (alias claude), gemini (aliases google, "
                "google-gemini), ollama, codex (alias openai-codex). Codex requires "
                "/codex-login first."
            ),
            examples=["/provider", "/provider claude", "/provider codex"],
            related=["/model", "/codex-login"],
        ),
    ),
    Command(
        names=["/model", "/models"],
        build=_build_model,
        help=HelpEntry(
            category="Providers & Models",
            summary="Choose a model or switch directly by name.",
            syntax=["/model", "/model <name>"],
            details=(
                "With no name, opens an interactive model picker and highlights "
                "the active model. `/models` is a compatibility alias. A direct "
                "model name switches without opening the picker."
            ),
            examples=[
                "/model",
                "/model gpt-5.6-sol",
                "/models gpt-5.6-terra",
            ],
            related=["/provider"],
        ),
    ),
    # --- Skills -------------------------------------------------------------
    Command(
        names=["/skill"],
        build=_build_skill,
        help=HelpEntry(
            category="Skills",
            summary="List skills, search them, or load one with optional input.",
            syntax=["/skill", "/skill search <query>", "/skill <name> [args]"],
            details=(
                "With no argument, lists available skills. 'search <query>' filters "
                "by keyword. '<name> [args]' loads a skill, optionally passing input "
                "to it."
            ),
            examples=[
                "/skill",
                "/skill search sqli",
                "/skill localhost-scan 10.0.0.1",
            ],
        ),
    ),
    # --- Engagements --------------------------------------------------------
    Command(
        names=["/engagements"],
        build=lambda parts, raw_input: EngagementsMessage(),
        help=HelpEntry(
            category="Engagements",
            summary="List saved engagements.",
            syntax=["/engagements"],
            details="Engagements persist as JSONL under ~/.kerkov/engagements/.",
            related=["/resume"],
        ),
    ),
    Command(
        names=["/resume"],
        build=_build_resume,
        help=HelpEntry(
            category="Engagements",
            summary="Resume a previous engagement.",
            syntax=["/resume", "/resume <id>"],
            details=(
                "With no argument, resumes the most recent engagement. Pass an id to "
                "resume a specific one (see /engagements for ids)."
            ),
            examples=["/resume", "/resume abc123"],
            related=["/engagements"],
        ),
    ),
    # --- Auth ---------------------------------------------------------------
    Command(
        names=["/auth", "/credentials"],
        build=_build_auth,
        help=HelpEntry(
            category="Auth",
            summary="Show, enter, or remove API credentials securely.",
            syntax=[
                "/auth",
                "/auth set <provider|tool|PROFILE_ENV_VAR> [--session]",
                "/auth remove <provider|tool|PROFILE_ENV_VAR>",
            ],
            details=(
                "Secret values are collected in a separate masked prompt and never "
                "belong in the command itself. By default credentials are saved in "
                "the operating-system keyring, or a user-only local file when no "
                "keyring is available. Pass --session to keep a value only until "
                "Kerkov exits."
            ),
            examples=[
                "/auth",
                "/auth set gemini",
                "/auth tavily",
                "/auth set openrouter --session",
                "/auth set HTB_API_TOKEN",
                "/auth remove HTB_API_TOKEN",
                "/auth remove gemini",
            ],
            related=["/provider", "/codex-login"],
        ),
    ),
    Command(
        names=["/codex-login"],
        build=lambda parts, raw_input: CodexLoginMessage(),
        help=HelpEntry(
            category="Auth",
            summary="Authenticate the Codex provider via browser OAuth.",
            syntax=["/codex-login"],
            details=(
                "Runs the Codex OAuth flow and persists tokens to "
                "data/providers/codex/auth.json. Run this before /provider codex."
            ),
            related=["/provider"],
        ),
    ),
]


_COMMAND_BY_NAME = {name: command for command in COMMANDS for name in command.names}


def find_command(token: str) -> Optional[Command]:
    """Look up a command by any of its names (slash optional, case-insensitive)."""
    bare = token.strip().lower().lstrip("/")
    candidates = {bare, "/" + bare}
    for command in COMMANDS:
        if candidates & set(command.names):
            return command
    return None
