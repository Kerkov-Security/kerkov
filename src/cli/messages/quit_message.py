from src.cli.messages.user_message import UserMessage

class QuitMessage(UserMessage):

    async def execute(self, cli):
        cli.quit()
