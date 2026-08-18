from kerkov.cli.messages.user_message import UserMessage


class QuitMessage(UserMessage):

    async def execute(self, cli):
        await cli.shutdown_agents()
        cli.quit()
