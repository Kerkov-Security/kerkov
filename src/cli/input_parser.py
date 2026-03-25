from src.cli.messages.clear_message import ClearMessage
from src.cli.messages.ctf_info_message import CtfInfoMessage
from src.cli.messages.codex_login_message import CodexLoginMessage
from src.cli.messages.general_message import GeneralMessage
from src.cli.messages.mode_info_message import ModeInfoMessge
from src.cli.messages.model_info_message import ModelInfoMessage
from src.cli.messages.switch_model_message import SwitchModelMessage
from src.cli.messages.models_message import ModelsMessage
from src.cli.messages.provider_info_message import ProviderInfoMessage
from src.cli.messages.switch_provider_message import SwitchProviderMessage
from src.cli.messages.providers_message import ProvidersMessage
from src.cli.messages.quit_message import QuitMessage
from src.cli.messages.switch_mode_message import SwitchModeMessage
from src.cli.messages.user_message import UserMessage

class InputParser:

    @staticmethod
    def parse_for_message_type(user_input: str) -> UserMessage:
        raw_input = user_input.strip()
        if not raw_input:
            return GeneralMessage("")

        parts = raw_input.split()
        command = parts[0].lower()

        if command == "/mode":
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
        
        return GeneralMessage("".join(raw_input.lower().split()))
        
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
