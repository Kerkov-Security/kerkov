"""Validate custom assessment checks and compile them into InSpec profiles."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kerkov.core.automation.profiles import confined_path, read_document
from kerkov.core.compliance.profile_sources import cache_generation_lock
from kerkov.core.private_files import write_private_json


class Check(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: Literal[
        "file_exists",
        "file_mode",
        "file_owner",
        "package_installed",
        "package_absent",
        "service_enabled",
        "service_disabled",
    ]
    target: str = Field(min_length=1, max_length=255)
    expected: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def validate_check(self) -> Check:
        if self.kind.startswith("file_"):
            if not re.fullmatch(r"/[A-Za-z0-9_./@+-]+", self.target):
                raise ValueError(
                    "Enter an absolute Linux file path with ordinary path characters"
                )
        elif not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@+-]*", self.target):
            raise ValueError("Enter an ordinary package or service name")
        if self.kind == "file_mode":
            if not re.fullmatch(r"0[0-7]{3}", self.expected):
                raise ValueError("Enter a four-digit octal mode such as 0644")
        elif self.kind == "file_owner":
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", self.expected):
                raise ValueError("Enter a Linux account name such as root")
        elif self.expected:
            raise ValueError("This check does not take an expected value")
        return self


class CustomProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=80)
    checks: list[Check] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def validate_profile(self) -> CustomProfile:
        if any(ord(char) < 32 or ord(char) == 127 for char in self.name):
            raise ValueError("Profile name must fit on one line")
        if len({(item.kind, item.target) for item in self.checks}) != len(self.checks):
            raise ValueError(
                "Each check type can be used once per file, package or service"
            )
        return self

    @property
    def revision(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()

    def render(self, profile_id: str, path: Path) -> None:
        # The YAML metadata is emitted as JSON and only validated data enters Ruby literals.
        (path / "inspec.yml").write_text(
            json.dumps(
                {
                    "name": f"kerkov-custom-{profile_id}",
                    "title": self.name,
                    "version": "1.0.0",
                    "supports": [{"platform-family": "linux"}],
                }
            ),
            encoding="utf-8",
        )
        (path / "controls").mkdir()
        (path / "controls/checks.rb").write_text(
            "\n\n".join(
                _render_check(index, check)
                for index, check in enumerate(self.checks, 1)
            )
            + "\n",
            encoding="utf-8",
        )


class CustomProfileRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")


def _render_check(index: int, check: Check) -> str:
    """Generate one fixed Linux resource assertion."""

    resource = check.kind.split("_", 1)[0]
    assertion = {
        "file_exists": "it { should be_file }",
        "file_mode": f"its('mode') {{ should cmp '{check.expected}' }}",
        "file_owner": f"its('owner') {{ should eq '{check.expected}' }}",
        "package_installed": "it { should be_installed }",
        "package_absent": "it { should_not be_installed }",
        "service_enabled": "it { should be_enabled }",
        "service_disabled": "it { should_not be_enabled }",
    }[check.kind]
    title = check.kind.replace("_", " ").capitalize()
    return "\n".join(
        (
            f"control 'custom-{index}' do",
            "  impact 0.5",
            f"  title '{title} · {check.target}'",
            f"  describe {resource}('{check.target}') do",
            f"    {assertion}",
            "  end",
            "end",
        )
    )


class CustomProfileStore:
    def __init__(self, projects_root: Path) -> None:
        self.root = confined_path(projects_root.expanduser().resolve(), "custom")

    def get(self, reference: CustomProfileRef) -> CustomProfile:
        profile = self._load(reference.id)
        if profile.revision != reference.revision:
            raise ValueError(
                "This profile changed. Refresh the picker and review the updated checks."
            )
        return profile

    def _load(self, profile_id: str) -> CustomProfile:
        if not re.fullmatch(r"[a-f0-9]{32}", profile_id):
            raise ValueError("Invalid custom profile ID")
        path = self.root / f"{profile_id}.json"
        if path.is_symlink() or not path.is_file():
            raise ValueError("Custom profile is unavailable")
        return CustomProfile.model_validate(read_document(path))

    def _entry(self, profile_id: str, profile: CustomProfile) -> dict:
        return {"id": profile_id, "revision": profile.revision, **profile.model_dump()}

    def list(self) -> list[dict]:
        if not self.root.is_dir():
            return []
        entries = []
        for path in sorted(self.root.glob("*.json")):
            try:
                entries.append(self._entry(path.stem, self._load(path.stem)))
            except (OSError, ValueError):
                continue
        return sorted(
            entries, key=lambda entry: (entry["name"].casefold(), entry["id"])
        )

    def save(self, profile: CustomProfile, reference: CustomProfileRef | None) -> dict:
        profile_id = reference.id if reference else uuid.uuid4().hex
        if reference is None:
            write_private_json(
                self.root / f"{profile_id}.json",
                profile.model_dump(),
                managed_parent=self.root,
                prefix=".profile-",
            )
        else:
            with cache_generation_lock(self.root, exclusive=True):
                self.get(reference)
                write_private_json(
                    self.root / f"{profile_id}.json",
                    profile.model_dump(),
                    managed_parent=self.root,
                    prefix=".profile-",
                )
        return self._entry(profile_id, profile)
