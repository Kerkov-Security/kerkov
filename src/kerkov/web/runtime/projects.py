"""Read-only catalog, cache and upstream URL checks for the compliance console."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

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
        projects = ProfileProjects(
            Path(session.config.compliance_projects_dir),
            image=session.config.cinc_docker_image,
        )
        if event == "compliance.projects":
            result = {
                "catalog": profile_catalog(),
                "projects": await asyncio.to_thread(projects.list),
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
    except TimeoutError:
        result = {
            "detail": "Project URL validation timed out. Check connectivity and retry."
        }
    except (OSError, ValueError, TypeError) as exc:
        result = {"detail": str(exc)}
    if not session._closed and session.inventory.context_id == context_id:
        session.bus.emit(
            event, {"context_id": context_id, "request_id": request_id, **result}
        )
