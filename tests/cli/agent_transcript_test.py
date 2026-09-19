"""Tests for the read-only rendering of a finished agent's transcript."""

import io
import unittest

from rich.console import Console

from kerkov.cli.agent_transcript import MESSAGE_CHARS, render_agent_transcript
from kerkov.cli.engagement_store import EngagementMetadata
from kerkov.models.basic_models import Message


def _create_mock_metadata(**overrides) -> EngagementMetadata:
    fields = dict(
        id="agent01",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:01:00+00:00",
        mode="agent",
        model="claude",
        verbosity="silent",
        approval_mode="auto",
        kind="agent",
        parent_engagement_id="conv01",
        parent_call_id="call-1",
        agent_type="recon",
        task_instruction="scan 10.0.0.5",
    )
    fields.update(overrides)
    return EngagementMetadata(**fields)


def _render(metadata, messages, **kwargs) -> str:
    buffer = io.StringIO()
    console = Console(file=buffer, width=120, no_color=True, legacy_windows=False)
    render_agent_transcript(console, metadata, messages, **kwargs)
    return buffer.getvalue()


class RenderAgentTranscriptTest(unittest.TestCase):

    def test_header_names_the_profile_task_and_parent(self):
        output = _render(_create_mock_metadata(), [])

        self.assertIn("recon", output)
        self.assertIn("scan 10.0.0.5", output)
        self.assertIn("conv01", output)

    def test_messages_are_printed_with_their_roles(self):
        messages = [
            Message(role="system", content="you are recon"),
            Message(role="assistant", content="found port 22"),
        ]

        output = _render(_create_mock_metadata(), messages)

        self.assertIn("assistant", output)
        self.assertIn("found port 22", output)

    def test_routing_selection_and_reason_are_escaped(self):
        metadata = _create_mock_metadata(
            provider="[bold]provider[/bold]",
            model="[red]model[/red]",
            routing_tier="small",
            routing_reason="[green]Explicit extraction[/green]",
        )

        output = _render(metadata, [])

        self.assertIn("[bold]provider[/bold] / [red]model[/red] (small)", output)
        self.assertIn("Routing: [green]Explicit extraction[/green]", output)

    def test_old_metadata_keeps_the_model_without_empty_routing_details(self):
        output = _render(_create_mock_metadata(), [])

        self.assertIn("Model: claude", output)
        self.assertNotIn("Routing:", output)
        self.assertNotIn("None", output)

    def test_only_the_tail_is_printed(self):
        messages = [Message(role="user", content=f"line-{i}") for i in range(10)]

        output = _render(_create_mock_metadata(), messages, tail=3)

        self.assertIn("line-9", output)
        self.assertNotIn("line-0", output)
        self.assertIn("7 earlier message(s) omitted", output)

    def test_a_huge_tool_result_is_clipped(self):
        messages = [Message(role="tool", content="x" * (MESSAGE_CHARS + 500))]

        output = _render(_create_mock_metadata(), messages)

        self.assertNotIn("x" * (MESSAGE_CHARS + 1), output)
        self.assertIn("…", output)

    def test_markup_in_a_tool_result_is_data_not_styling(self):
        messages = [Message(role="tool", content="[bold]not a style[/bold]")]

        output = _render(_create_mock_metadata(), messages)

        self.assertIn("[bold]not a style[/bold]", output)

    def test_the_reader_is_told_it_cannot_continue_the_run(self):
        output = _render(_create_mock_metadata(), [])

        self.assertIn("Read-only", output)


if __name__ == "__main__":
    unittest.main()
