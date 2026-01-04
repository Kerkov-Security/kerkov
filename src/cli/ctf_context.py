"""
CTF Challenge context management.

Stores structured information about CTF challenges to provide
better context to the AI.
"""

from dataclasses import dataclass
from typing import Optional

@dataclass
class CTFContext:
    """
    Stores structured information about a CTF challenge.

    Attributes:
        challenge_name: Name of the CTF challenge
        description: Full description of the challenge
        category: Challenge category (web, crypto, pwn, forensics, reversing, misc)
        target: Target location (URI, file path, IP address, etc.)
        additional_notes: Any additional context or notes
    """
    challenge_name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    target: Optional[str] = None
    additional_notes: Optional[str] = None
    write_up_dir: Optional[str] = None

    def has_context(self) -> bool:
        """Check if any context information is provided."""
        return any([
            self.challenge_name,
            self.description,
            self.category,
            self.target,
            self.additional_notes,
            self.write_up_dir,
        ])

    def to_prompt_string(self) -> str:
        """
        Convert CTF context to a formatted string for inclusion in prompts.

        Returns:
            Formatted string with challenge information
        """
        if not self.has_context():
            return ""

        parts = ["\n--- CTF CHALLENGE INFORMATION ---"]

        if self.challenge_name:
            parts.append(f"Challenge Name: {self.challenge_name}")

        if self.category:
            parts.append(f"Category: {self.category}")

        if self.target:
            parts.append(f"Target: {self.target}")

        if self.description:
            parts.append(f"\nDescription:\n{self.description}")

        if self.additional_notes:
            parts.append(f"\nAdditional Notes:\n{self.additional_notes}")

        if self.write_up_dir:
            parts.append("\nOnce the challenge has been completed (flag found, objective achieved, or you deem it complete), "
            \
                        "please generate a comprehensive writeup of the challenge solution in a markdown file. " \
                        "The writeup should document your methodology and include:\n" \
                        "- Initial reconnaissance and enumeration steps\n" \
                        "- Vulnerability identification and analysis\n" \
                        "- Exploitation techniques and tools used\n" \
                        "- Key findings (e.g., foothold, privilege escalation, flag location)\n" \
                        "- Brief explanations of important concepts and methodologies\n" \
                        "- Any alternative approaches or interesting observations\n\n" \
                        "The writeup must be saved in the writeup directory with a filename based on the challenge name. " \
                        "If the challenge name contains invalid filename characters, create a suitable sanitized filename.\n")
            parts.append(f"Writeup Directory: {self.write_up_dir}")

        parts.append("--- END CHALLENGE INFORMATION ---\n")

        return "\n".join(parts)

    def clear(self) -> None:
        """Clear all context information."""
        self.challenge_name = None
        self.description = None
        self.category = None
        self.target = None
        self.additional_notes = None
        self.write_up_dir = None
