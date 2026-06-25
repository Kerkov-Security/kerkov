from src.cli.messages.user_message import UserMessage


class GeneralMessage(UserMessage):

    def __init__(self, user_input: str):
        self.user_input = user_input

    async def execute(self, cli):
        await cli.get_response(self.user_input)
