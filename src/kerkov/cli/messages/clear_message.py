from kerkov.cli.messages.user_message import UserMessage


class ClearMessage(UserMessage):

    async def execute(self, cli):
        await cli.settle_agents_for_conversation_switch()
        cli.clear_history()
