from src.cli.messages.clear_message import ClearMessage
from src.cli.messages.ctf_info_message import CtfInfoMessage
from src.cli.messages.general_message import GeneralMessage
from src.cli.messages.mode_info_message import ModeInfoMessge
from src.cli.messages.quit_message import QuitMessage
from src.cli.messages.switch_mode_message import SwitchModeMessage
from src.cli.messages.shell_message import ShellMessage
from src.cli.messages.user_message import UserMessage

class InputParser:

    @staticmethod
    def parse_for_message_type(user_input: str) -> UserMessage:
        user_input: list[str] = user_input.lower().strip().split()
        if user_input[0] == "/mode":
            return _determine_mode_message(user_input)
        elif user_input[0] == "/clear":
            return ClearMessage()
        elif user_input[0] in ["/q", "quit", "exit"]:
            return QuitMessage()
        elif user_input[0] == "/ctf-info":
            return CtfInfoMessage()
        elif user_input[0] == "/shell":
            return ShellMessage()
        
        return GeneralMessage("".join(user_input))
        
def _determine_mode_message(mode_msg: list[str]) -> UserMessage:
    if len(mode_msg) == 1:
        return ModeInfoMessge()
    
    return SwitchModeMessage(mode_msg[1])
