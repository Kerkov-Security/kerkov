"""
Verbosity-level prompt fragments for Kerkov Bot.

Appended after the mode-specific system prompt overlay to control how verbose
the LLM is about its reasoning and tool usage. Approval gating is orthogonal
and lives in `approval_mode_manager.py`.
"""

from typing import List

from kerkov.cli.verbosity_manager import Verbosity
from kerkov.models.basic_models import Message

VERBOSITY_MARKER = "\n<!-- VERBOSITY -->\n"

VERBOSITY_PROMPTS: dict[Verbosity, str] = {
    Verbosity.GUIDED: """
COMMUNICATION STYLE:
- Before EVERY tool call, provide a detailed explanation of:
  - What you are about to do and why
  - What you expect the result to tell you
  - How it fits into your overall approach
- Explain each tool call individually, not as a batch.
- After tool results, briefly interpret what you learned before proceeding.
""",
    Verbosity.STANDARD: """
COMMUNICATION STYLE:
- Before executing tools, provide a brief explanation of your approach and what
  you plan to do.
- When executing multiple tools, summarize the batch rather than explaining each
  individually.
- After completing a sequence of tool calls, summarize findings before moving on.
""",
    Verbosity.AGGRESSIVE: """
COMMUNICATION STYLE:
- Only explain your high-level intent when starting a new phase or multi-step sequence.
- Do not explain individual tool calls.
- Focus output on results and findings, not process.
""",
    Verbosity.SILENT: """
COMMUNICATION STYLE:
- Execute tools without explanation. Focus entirely on results.
- Only explain your methodology if the user explicitly asks.
- Do NOT embed narration as `#` comments inside tool `command` strings or other
  tool parameters. Tool arguments must contain only what the tool will actually
  execute — no descriptive comments, no "what this does" annotations.
""",
}


def apply_verbosity_to_system_prompt(
    conversation_history: List[Message], verbosity: Verbosity
) -> None:
    """Swap the verbosity fragment in the conversation's system message in place.

    The system prompt's verbosity fragment is set at engagement start, but
    later `/verbosity` switches or resumes need to keep it in sync with the
    active verbosity.
    """
    if not conversation_history:
        return
    system_msg = conversation_history[0]
    if system_msg.role != "system":
        return
    content = system_msg.content.split(VERBOSITY_MARKER, 1)[0]
    content += VERBOSITY_MARKER + VERBOSITY_PROMPTS[verbosity]
    system_msg.content = content
