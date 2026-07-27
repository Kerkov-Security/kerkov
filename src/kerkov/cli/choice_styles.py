"""Shared formatting for interactive choice prompts."""

from prompt_toolkit.styles import Style

SELECTED_CHOICE_STYLE = "bold fg:ansicyan"
CHOICE_INPUT_STYLE = Style.from_dict(
    {
        "frame.border": "#884444",
        "selected-option": SELECTED_CHOICE_STYLE,
    }
)
