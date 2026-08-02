"""
SkillTool — LLM-invocable tool for listing, loading, and searching skills.
"""

from typing import Optional

from pydantic import Field

from kerkov.context.skills.models import IneligibilityReason
from kerkov.context.skills.skill_manager import SkillManager
from kerkov.core.credentials import CredentialManager, CredentialPrompt
from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult


class SkillToolParams(ToolParams):
    action: str = Field(description="Action to perform: 'list', 'load', or 'search'")
    skill_id: Optional[str] = Field(
        default=None,
        description="Skill name to load (required for 'load' action)",
    )
    query: Optional[str] = Field(
        default=None,
        description="Search query string (for 'search' action)",
    )
    tags: Optional[list[str]] = Field(
        default=None,
        description="Filter by tags (for 'search' action)",
    )
    category: Optional[str] = Field(
        default=None,
        description="Filter by category (for 'search' action)",
    )


class SkillTool(BaseTool):
    """Look up security skill files: list available skills, load a skill's full instructions, or search by keyword."""

    def __init__(
        self,
        skill_manager: SkillManager,
        *,
        credentials: Optional[CredentialManager] = None,
        credential_prompt: Optional[CredentialPrompt] = None,
    ):
        super().__init__(
            name="skill_lookup",
            description=(
                "Look up security skill files. Actions: "
                "'list' returns all available skills, "
                "'load' returns the full instructions for a skill (requires skill_id), "
                "'search' finds skills matching a query/tags/category."
            ),
        )
        self._skill_manager = skill_manager
        self._credentials = credentials
        self._credential_prompt = credential_prompt

    def get_params_class(self):
        return SkillToolParams

    def validate_params(self, toolParams: SkillToolParams) -> Optional[str]:
        if toolParams.action not in ("list", "load", "search"):
            return f"Invalid action: '{toolParams.action}'. Must be 'list', 'load', or 'search'."
        if toolParams.action == "load" and not toolParams.skill_id:
            return "skill_id is required for 'load' action."
        if toolParams.action == "search" and not any(
            [toolParams.query, toolParams.tags, toolParams.category]
        ):
            return "At least one of query, tags, or category is required for 'search' action."
        return None

    async def run(self, toolParams: SkillToolParams) -> ToolResult:
        if toolParams.action == "list":
            return self._handle_list()
        elif toolParams.action == "load":
            return await self._handle_load(toolParams.skill_id)
        elif toolParams.action == "search":
            return self._handle_search(
                toolParams.query, toolParams.tags, toolParams.category
            )
        # Should not reach here due to validate_params
        return ToolResult(success=False, output="", error="Unknown action")

    def _handle_list(self) -> ToolResult:
        skills = self._skill_manager.list_skills()
        if not skills:
            return ToolResult(success=True, output="No skills available.")

        lines = []
        for entry in skills:
            eligibility = self._skill_manager.check_eligibility(entry)
            difficulty = ""
            if entry.kerkov and entry.kerkov.difficulty:
                difficulty = f" [{entry.kerkov.difficulty}]"
            ineligible = ""
            if not eligibility.eligible:
                parts = []
                if IneligibilityReason.MISSING_BINS in eligibility.reasons:
                    parts.append(f"missing: {', '.join(eligibility.missing_bins)}")
                if IneligibilityReason.NEEDS_ELEVATION in eligibility.reasons:
                    parts.append("needs elevated privileges")
                ineligible = f" ({'; '.join(parts)})"
            lines.append(f"- {entry.name}: {entry.description}{difficulty}{ineligible}")

        return ToolResult(
            success=True,
            output="\n".join(lines),
            metadata={"count": len(skills)},
        )

    async def _handle_load(self, skill_id: str) -> ToolResult:
        entry = self._skill_manager.get_skill(skill_id)
        if not entry:
            return ToolResult(
                success=False,
                output="",
                error=f"Skill '{skill_id}' not found.",
            )

        if entry.disable_model_invocation:
            return ToolResult(
                success=False,
                output="",
                error=f"Skill '{skill_id}' has model invocation disabled.",
            )

        eligibility = self._skill_manager.check_eligibility(entry)

        if IneligibilityReason.NEEDS_ELEVATION in eligibility.reasons:
            return ToolResult(
                success=False,
                output="",
                error=(
                    f"Skill '{skill_id}' requires elevated privileges, but "
                    f"the kerkov process is not running as root (POSIX) or "
                    f"Administrator (Windows). Restart kerkov with `sudo` on "
                    f"Linux/macOS or from an elevated terminal on Windows, "
                    f"then retry. Alternatively, grant the underlying binaries "
                    f"the capabilities they need (e.g. `setcap` for openvpn) "
                    f"so elevation isn't required."
                ),
                metadata={
                    "skill_id": entry.name,
                    "eligible": False,
                    "reasons": [r.value for r in eligibility.reasons],
                },
            )

        required_credentials = (
            entry.kerkov.requires.credentials
            if entry.kerkov
            and entry.kerkov.requires
            and entry.kerkov.requires.credentials
            else []
        )
        if required_credentials:
            if self._credentials is None:
                missing = required_credentials[0]
                return ToolResult(
                    success=False,
                    output="",
                    error=(
                        f"Skill '{skill_id}' requires {missing}. "
                        f"Run `/auth set {missing}` before loading it."
                    ),
                )
            ensured = await self._credentials.ensure_environment_credentials(
                required_credentials,
                self._credential_prompt,
                context=f"skill '{entry.name}'",
            )
            if not ensured.ready:
                assert ensured.missing is not None
                return ToolResult(
                    success=False,
                    output="",
                    error=(
                        f"Skill '{skill_id}' requires {ensured.missing.env_var}. "
                        f"Run `/auth set {ensured.missing.env_var}` or enter it "
                        "when prompted."
                    ),
                )

        header = (
            f"# Skill: {entry.name}\n"
            f"**Location:** `{entry.skill_dir}`\n"
            f"Resolve any relative paths in this skill "
            f"(e.g. `scripts/foo.py`) against this directory.\n\n"
        )
        if eligibility.missing_bins:
            header += (
                f"**Warning:** Missing required binaries: "
                f"{', '.join(eligibility.missing_bins)}\n\n"
            )

        metadata = {"skill_id": entry.name, "eligible": eligibility.eligible}
        if required_credentials:
            metadata["credentials"] = {
                env_var: "configured" for env_var in required_credentials
            }

        return ToolResult(
            success=True,
            output=header + entry.content,
            metadata=metadata,
        )

    def _handle_search(
        self,
        query: Optional[str],
        tags: Optional[list[str]],
        category: Optional[str],
    ) -> ToolResult:
        results = self._skill_manager.search(query=query, tags=tags, category=category)
        if not results:
            return ToolResult(
                success=True, output="No skills matched the search criteria."
            )

        lines = []
        for entry in results:
            difficulty = ""
            if entry.kerkov and entry.kerkov.difficulty:
                difficulty = f" [{entry.kerkov.difficulty}]"
            lines.append(f"- {entry.name}: {entry.description}{difficulty}")

        return ToolResult(
            success=True,
            output="\n".join(lines),
            metadata={"count": len(results)},
        )
