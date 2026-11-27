"""Profile and remediation imports for the compliance console."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kerkov.core.compliance.community_fixes import CommunityFixStore
from kerkov.core.compliance.custom_profiles import (
    CustomProfile,
    CustomProfileRef,
    CustomProfileStore,
)
from kerkov.core.compliance.profile_sources import (
    COMMIT_PATTERN,
    ProfileSource,
    profile_catalog,
)
from kerkov.core.compliance.projects import ProfileProjects

if TYPE_CHECKING:
    from kerkov.web.runtime.session import WebKerkovSession


class ProjectPreparationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    url: str = Field(max_length=2048)
    refresh: bool = False
    commit: str = Field(pattern=f"^(?:{COMMIT_PATTERN.pattern})$")


class CustomProfileSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: CustomProfile
    reference: CustomProfileRef | None = None


async def read_projects(session: WebKerkovSession, content: dict[str, Any]) -> None:
    context_id = session.inventory.context_id
    request_id = content.get("request_id")
    if (
        content.get("context_id") != context_id
        or not isinstance(request_id, str)
        or not 1 <= len(request_id) <= 128
    ):
        return
    event = content["type"]
    result: dict[str, Any]
    try:
        root = Path(session.config.compliance_projects_dir)
        projects = ProfileProjects(
            root,
            image=session.config.cinc_docker_image,
        )
        if event in {
            "compliance.fixes",
            "compliance.fix.import",
            "compliance.fix.review",
        }:
            fixes = CommunityFixStore(root)
            if event == "compliance.fix.import":
                fix_id = content.get("fix_id")
                result = {
                    "available": await fixes.import_fix(fix_id),
                    "fix": await asyncio.to_thread(fixes.review, fix_id),
                }
            elif event == "compliance.fix.review":
                result = {
                    "fix": await asyncio.to_thread(fixes.review, content.get("fix_id"))
                }
            else:
                result = {"available": await asyncio.to_thread(fixes.list)}
        elif event == "compliance.projects":
            result = {
                "catalog": profile_catalog(),
                "projects": await asyncio.to_thread(projects.list),
                "custom_profiles": await asyncio.to_thread(
                    CustomProfileStore(root).list
                ),
            }
        elif event == "compliance.custom.save":
            save = CustomProfileSaveRequest.model_validate(content.get("draft"))
            result = {
                "profile": await asyncio.to_thread(
                    CustomProfileStore(root).save, save.profile, save.reference
                )
            }
        else:
            source = ProfileSource.parse(content.get("url"))
            if event == "compliance.project.validate":
                async with asyncio.timeout(30):
                    validated = await projects.remote.validate(source.url)
                result = {"validated": validated}
            else:
                result = {
                    "url": source.url,
                    "project": await asyncio.to_thread(
                        lambda: projects.get(source.url, verify=True)
                    ),
                }
    except ValidationError as exc:
        error = exc.errors(
            include_url=False, include_input=False, include_context=False
        )[0]
        result = {"detail": error["msg"].removeprefix("Value error, ")}
    except TimeoutError:
        result = {"detail": "The request timed out. Check connectivity and retry."}
    except (OSError, ValueError, TypeError) as exc:
        result = {"detail": str(exc)}
    if not session._closed and session.inventory.context_id == context_id:
        session.bus.emit(
            event, {"context_id": context_id, "request_id": request_id, **result}
        )
