from src.cli.messages.user_message import UserMessage


class ClearMessage(UserMessage):

    async def execute(self, cli):
        cli.clear_history()
