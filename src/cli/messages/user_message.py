from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.cli.kerkov_cli import KerkovCLI


class UserMessage(ABC):

    @abstractmethod
    async def execute(self, cli: "KerkovCLI"):
        """Execute a user message."""
        pass
