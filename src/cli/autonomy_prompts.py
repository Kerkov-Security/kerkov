"""
Autonomy-level prompt fragments for Kerkov Bot.

Appended after the mode-specific system prompt overlay to control how verbose
the LLM is about its reasoning and tool usage.
"""

from src.cli.autonomy_manager import AutonomyLevel

AUTONOMY_PROMPTS: dict[AutonomyLevel, str] = {
    AutonomyLevel.GUIDED: """
COMMUNICATION STYLE:
- Before EVERY tool call, provide a detailed explanation of:
  - What you are about to do and why
  - What you expect the result to tell you
  - How it fits into your overall approach
- Explain each tool call individually, not as a batch.
- After tool results, briefly interpret what you learned before proceeding.
""",
    AutonomyLevel.STANDARD: """
COMMUNICATION STYLE:
- Before executing tools, provide a brief explanation of your approach and what
  you plan to do.
- When executing multiple tools, summarize the batch rather than explaining each
  individually.
- After completing a sequence of tool calls, summarize findings before moving on.
""",
    AutonomyLevel.AGGRESSIVE: """
COMMUNICATION STYLE:
- Only explain your high-level intent when starting a new phase or multi-step sequence.
- Do not explain individual tool calls.
- Focus output on results and findings, not process.
""",
    AutonomyLevel.AUTONOMOUS: """
COMMUNICATION STYLE:
- Execute tools without explanation. Focus entirely on results.
- Only explain your methodology if the user explicitly asks.
""",
}
