from kerkov.cli.messages.user_message import UserMessage


class SwitchProviderMessage(UserMessage):

    def __init__(self, new_provider_name: str):
        super().__init__()
        self.new_provider_name = new_provider_name

    async def execute(self, cli):
        await cli.activate_provider_interactive(self.new_provider_name)
