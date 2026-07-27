"""Shared formatting for interactive choice labels."""

from prompt_toolkit.formatted_text import FormattedText

ACTIVE_CHOICE_STYLE = "bold fg:ansicyan"


def active_choice_label(label: str) -> FormattedText:
    """Color an option that represents the currently active setting."""

    return FormattedText([(ACTIVE_CHOICE_STYLE, label)])
