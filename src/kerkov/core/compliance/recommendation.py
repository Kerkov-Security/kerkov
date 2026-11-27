"""Pick a Linux profile for a verified platform and an operator-selected goal."""

import re
import shlex
from typing import Literal

from kerkov.core.compliance.scap_catalog import scap_catalog

AssessmentGoal = Literal["hardening", "cis", "stig", "pci"]

_OS_RELEASE_PREFIX = "KERKOV_OS_RELEASE="
OS_RELEASE_PROBE = (
    f"""printf '{_OS_RELEASE_PREFIX}%s\\n' "$(uname -s)" && cat /etc/os-release"""
)

_STIG = {
    ("ubuntu", "22.04"): "mitre-ubuntu-2204-stig",
    ("ubuntu", "24.04"): "mitre-ubuntu-2404-stig",
    ("rhel", "8"): "mitre-rhel-8-stig",
    ("rhel", "9"): "mitre-rhel-9-stig",
}

_GOALS = {
    "hardening": "general Linux hardening",
    "cis": "CIS Linux",
    "stig": "DISA STIG",
    "pci": "PCI DSS v4.0.1",
}


def recommend_profile(output: str, goal: AssessmentGoal) -> dict[str, str]:
    """Use fixed Linux detection output and compatible catalog entries."""

    lines = output.splitlines()
    headers = [i for i, line in enumerate(lines) if line.startswith(_OS_RELEASE_PREFIX)]
    # The process result also includes SSH diagnostics from stderr.
    if (
        len(output) > 16_384
        or len(headers) != 1
        or lines[headers[0]] != f"{_OS_RELEASE_PREFIX}Linux"
    ):
        raise ValueError(
            "Auto could not confirm a Linux host. Choose a profile manually."
        )
    fields = {}
    for line in lines[headers[0] + 1 :]:
        key, separator, raw = line.partition("=")
        if not separator or key not in {"ID", "VERSION_ID"}:
            continue
        try:
            values = shlex.split(raw)
        except ValueError as exc:
            raise ValueError(
                "The host returned invalid OS release information."
            ) from exc
        if (
            key in fields
            or len(values) != 1
            or not re.fullmatch(r"[a-zA-Z0-9._-]{1,64}", values[0])
        ):
            raise ValueError("The host returned invalid OS release information.")
        fields[key] = values[0]
    distro = fields.get("ID", "").lower()
    version = fields.get("VERSION_ID", "")
    if not distro:
        raise ValueError(
            "Auto could not identify the Linux distribution. Choose a profile manually."
        )
    platform = " ".join(
        filter(
            None,
            (
                {"ubuntu": "Ubuntu", "debian": "Debian", "rhel": "RHEL"}.get(
                    distro, distro
                ),
                version,
            ),
        )
    )
    major = version.partition(".")[0]
    if goal == "stig":
        profile_id = _STIG.get((distro, major if distro == "rhel" else version))
    elif goal == "pci":
        profile_id = next(
            (
                item["id"]
                for item in scap_catalog()
                if distro == "rhel" and item["openscap"]["version"] == f"{major}.*"
            ),
            None,
        )
    else:
        profile_id = "devsec-cis-linux" if goal == "cis" else "devsec-linux"
    if not profile_id:
        raise ValueError(
            f"No {_GOALS[goal]} profile in the catalog matches {platform}. Choose a profile manually."
        )
    return {
        "id": profile_id,
        "platform": platform,
        "reason": f"Verified {platform} over the selected SSH endpoint. This profile matches the {_GOALS[goal]} goal.",
    }
