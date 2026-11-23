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
            "version": "1.0.0",
            "description": "Protect system account and password files with the correct ownership and permissions.",
            "platform": "Debian / Ubuntu",
            "controls": [
                "passwd-protected",
                "group-protected",
                "shadow-protected",
                "gshadow-protected",
            ],
            "changes": "Sets ownership and permissions on /etc/passwd, /etc/group, /etc/shadow and /etc/gshadow. Installs Python 3 and its required packages only if needed for remediation.",
            "profile": str(root / "linux-accounts"),
            "playbook": str(root / "linux-accounts" / "remediate.yml"),
            "profile_name": "kerkov-linux-accounts",
            "remediation": {
                "passwd-protected": "Set /etc/passwd ownership to root:root and mode to 0644.",
                "group-protected": "Set /etc/group ownership to root:root and mode to 0644.",
                "shadow-protected": "Set /etc/shadow ownership to root:shadow and mode to 0640.",
                "gshadow-protected": "Set /etc/gshadow ownership to root:shadow and mode to 0640.",
            },
            "recommended": True,
        },
        {
            "id": "ssh-essentials",
            "name": "SSH essentials",
            "version": "1.1.0",
            "description": "Tighten global SSH settings while preserving your existing login methods.",
            "platform": "Debian / Ubuntu · systemd + OpenSSH",
            "controls": [
                "ssh-no-empty-passwords",
                "ssh-ignore-rhosts",
                "ssh-no-x11",
                "ssh-auth-retries",
                "ssh-login-grace",
            ],
            "changes": "Disables empty passwords and X11 forwarding, ignores rhosts, limits authentication attempts to at most 4 and login grace to at most 60 seconds. Installs Python 3 only if needed for remediation. Preserves stricter positive limits. Validates configuration before reloading SSH. Preserves root/password login. Assesses global settings; Match-specific overrides need separate review.",
            "profile": str(root / "ssh-essentials"),
            "playbook": str(root / "ssh-essentials" / "remediate.yml"),
            "profile_name": "kerkov-ssh-essentials",
            "remediation": {
                "ssh-no-empty-passwords": "Set global PermitEmptyPasswords to no.",
                "ssh-ignore-rhosts": "Set global IgnoreRhosts to yes.",
                "ssh-no-x11": "Set global X11Forwarding to no.",
                "ssh-auth-retries": "Limit global MaxAuthTries to 4, preserving stricter positive limits.",
                "ssh-login-grace": "Limit global LoginGraceTime to 60 seconds, preserving stricter positive limits.",
            },
            "recommended": False,
        },
    ]
