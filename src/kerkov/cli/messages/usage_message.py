"""Read the saved usage of an engagement and its delegated agents."""

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from kerkov.cli.messages.user_message import UserMessage
from kerkov.routing.measurement import (
    format_summary,
    load_prices,
    summarize_engagement,
)

if TYPE_CHECKING:
    from kerkov.cli.kerkov_cli import KerkovCLI


class UsageMessage(UserMessage):
    """`/usage [engagement-id]` — usage and optional estimated costs."""

    def __init__(self, engagement_id: Optional[str] = None, *, invalid: bool = False):
        self.engagement_id = engagement_id
        self.invalid = invalid

    async def execute(self, cli: "KerkovCLI") -> None:
        console = cli.console
        if self.invalid:
            console.print(
                "Usage: /usage [engagement-id]", markup=False, highlight=False
            )
            return

        engagement_id = self.engagement_id or cli.engagement_store.engagement_id
        if not engagement_id:
            console.print(
                "No active engagement. Start a conversation or use /usage <id>; "
                "/engagements lists saved IDs.",
                markup=False,
                highlight=False,
            )
            return

        prices = None
        pricing_file = cli.config.model_pricing_file
        if pricing_file:
            try:
                prices = await asyncio.to_thread(
                    load_prices, Path(pricing_file).expanduser()
                )
            except (OSError, ValueError) as exc:
                console.print(
                    f"Could not load MODEL_PRICING_FILE: {exc}. "
                    "Showing usage with unknown costs.",
                    markup=False,
                    highlight=False,
                )

        try:
            summary = await asyncio.to_thread(
                summarize_engagement,
                cli.engagement_store.store_dir,
                engagement_id,
                prices,
            )
        except (OSError, ValueError) as exc:
            console.print(
                f"Could not read usage: {exc}. Use /engagements to find saved IDs.",
                markup=False,
                highlight=False,
            )
            return

        console.print(format_summary(summary), markup=False, highlight=False)
