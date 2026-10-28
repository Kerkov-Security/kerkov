"""Built-in assessment profiles paired with their explicit remediation playbooks."""

from pathlib import Path
from typing import Any

import kerkov.config


def builtin_baselines() -> list[dict[str, Any]]:
    root = Path(kerkov.config.__file__).resolve().parent / "compliance"
    return [
        {
            "id": "linux-accounts",
            "name": "Linux account protection",
            "description": "Protect system account and password files with the correct ownership and permissions.",
            "platform": "Debian / Ubuntu",
            "controls": [
                "passwd-protected",
                "group-protected",
                "shadow-protected",
                "gshadow-protected",
            ],
            "changes": "Sets ownership and permissions on /etc/passwd, /etc/group, /etc/shadow and /etc/gshadow. No packages or services are changed.",
            "profile": str(root / "linux-accounts"),
            "playbook": str(root / "linux-accounts" / "remediate.yml"),
            "profile_name": "kerkov-linux-accounts",
            "recommended": True,
        },
        {
            "id": "ssh-essentials",
            "name": "SSH essentials",
            "description": "Tighten global SSH settings while preserving your existing login methods.",
            "platform": "Debian / Ubuntu · systemd + OpenSSH",
            "controls": [
                "ssh-no-empty-passwords",
                "ssh-ignore-rhosts",
                "ssh-no-x11",
                "ssh-auth-retries",
                "ssh-login-grace",
            ],
            "changes": "Disables empty passwords and X11 forwarding, ignores rhosts, limits authentication attempts to 4 and login grace to 60 seconds. Validates configuration before reloading SSH. Preserves root/password login. Assesses global settings; Match-specific overrides need separate review.",
            "profile": str(root / "ssh-essentials"),
            "playbook": str(root / "ssh-essentials" / "remediate.yml"),
            "profile_name": "kerkov-ssh-essentials",
            "recommended": False,
        },
    ]
