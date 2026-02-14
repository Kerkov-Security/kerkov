from src.cli.messages.user_message import UserMessage

class SwitchProviderMessage(UserMessage):

    def __init__(self, new_provider_name: str):
        super().__init__()
        self.new_provider_name = new_provider_name

    async def execute(self, cli):
        cli.provder_manager.switch_provider(self.new_provider_name)
