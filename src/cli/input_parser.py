from src.cli.messages.approval_info_message import ApprovalInfoMessage
from src.cli.messages.autonomy_info_message import AutonomyInfoMessage
from src.cli.messages.clear_message import ClearMessage
from src.cli.messages.codex_login_message import CodexLoginMessage
from src.cli.messages.ctf_info_message import CtfInfoMessage
from src.cli.messages.engagements_message import EngagementsMessage
from src.cli.messages.general_message import GeneralMessage
from src.cli.messages.mode_info_message import ModeInfoMessge
from src.cli.messages.model_info_message import ModelInfoMessage
from src.cli.messages.switch_approval_message import SwitchApprovalMessage
from src.cli.messages.switch_model_message import SwitchModelMessage
from src.cli.messages.switch_verbosity_message import SwitchVerbosityMessage
from src.cli.messages.models_message import ModelsMessage
from src.cli.messages.provider_info_message import ProviderInfoMessage
from src.cli.messages.resume_message import ResumeMessage
from src.cli.messages.skill_message import SkillListMessage, SkillLoadMessage, SkillSearchMessage
from src.cli.messages.switch_provider_message import SwitchProviderMessage
from src.cli.messages.providers_message import ProvidersMessage
from src.cli.messages.quit_message import QuitMessage
from src.cli.messages.switch_autonomy_message import SwitchAutonomyMessage
from src.cli.messages.switch_mode_message import SwitchModeMessage
from src.cli.messages.user_message import UserMessage
from src.cli.messages.verbosity_info_message import VerbosityInfoMessage
from src.cli.messages.yolo_message import YoloMessage

class InputParser:

    @staticmethod
    def parse_for_message_type(user_input: str) -> UserMessage:
        raw_input = user_input.strip()
        if not raw_input:
            return GeneralMessage("")

        parts = raw_input.split()
        command = parts[0].lower()

        if command == "/autonomy":
            return _determine_autonomy_message(parts)
        elif command == "/verbosity":
            return _determine_verbosity_message(parts)
        elif command == "/approval":
            return _determine_approval_message(parts)
        elif command == "/mode":
            return _determine_mode_message([p.lower() for p in parts])
        elif command == "/model":
            return _determine_model_message(parts)
        elif command == "/models":
            return ModelsMessage()
        elif command == "/provider":
            return _determine_provider_message(parts)
        elif command == "/providers":
            return ProvidersMessage()
        elif command == "/clear":
            return ClearMessage()
        elif command in ["/q", "quit", "exit"]:
            return QuitMessage()
        elif command == "/ctf-info":
            return CtfInfoMessage()
        elif command == "/codex-login":
            return CodexLoginMessage()
        elif command == "/engagements":
            return EngagementsMessage()
        elif command == "/resume":
            return _determine_resume_message(parts)
        elif command == "/skill":
            return _determine_skill_message(parts)
        elif command == "/yolo":
            return YoloMessage(parts[1] if len(parts) > 1 else None)

        return GeneralMessage(raw_input)

def _determine_mode_message(mode_msg: list[str]) -> UserMessage:
    if len(mode_msg) == 1:
        return ModeInfoMessge()

    return SwitchModeMessage(mode_msg[1])

def _determine_model_message(parts: list[str]) -> UserMessage:
    if len(parts) == 1:
        return ModelInfoMessage()
    return SwitchModelMessage(" ".join(parts[1:]).strip())

def _determine_provider_message(parts: list[str]) -> UserMessage:
    if len(parts) == 1:
        return ProviderInfoMessage()
    return SwitchProviderMessage(" ".join(parts[1:]).strip())

def _determine_autonomy_message(parts: list[str]) -> UserMessage:
    if len(parts) == 1:
        return AutonomyInfoMessage()
    return SwitchAutonomyMessage(parts[1].strip())

def _determine_verbosity_message(parts: list[str]) -> UserMessage:
    if len(parts) == 1:
        return VerbosityInfoMessage()
    return SwitchVerbosityMessage(parts[1].strip())

def _determine_approval_message(parts: list[str]) -> UserMessage:
    if len(parts) == 1:
        return ApprovalInfoMessage()
    return SwitchApprovalMessage(parts[1].strip())

def _determine_resume_message(parts: list[str]) -> UserMessage:
    if len(parts) == 1:
        return ResumeMessage()
    return ResumeMessage(parts[1].strip())

def _determine_skill_message(parts: list[str]) -> UserMessage:
    if len(parts) == 1:
        return SkillListMessage()
    if parts[1].lower() == "search" and len(parts) > 2:
        return SkillSearchMessage(" ".join(parts[2:]).strip())
    skill_id = parts[1].strip()
    skill_args = " ".join(parts[2:]).strip() if len(parts) > 2 else None
    return SkillLoadMessage(skill_id, skill_args)
