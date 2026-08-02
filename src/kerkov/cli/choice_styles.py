"""Shared formatting for interactive choice prompts."""

from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.styles import Style

SELECTED_CHOICE_STYLE = "bold fg:ansicyan"
ACTIVE_CHOICE_STYLE = "bold fg:ansigreen"
CHOICE_INPUT_STYLE = Style.from_dict(
    {
        "frame.border": "#884444",
        "selected-option": SELECTED_CHOICE_STYLE,
        "active-choice": ACTIVE_CHOICE_STYLE,
        "selected-option active-choice": ACTIVE_CHOICE_STYLE,
    }
)


def active_choice_label(label: str) -> FormattedText:
    """Format the currently active choice independently from picker focus."""

    return FormattedText([("class:active-choice", label)])
