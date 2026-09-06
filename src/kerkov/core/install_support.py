"""Post-install setup, run with the Python interpreter in the tool environment."""

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from kerkov.core.private_files import prepare_private_parent
from kerkov.core.project_paths import bundled_asset_path

USER_APPROVALS_TEMPLATE = b"""# User approval overrides (first match wins).
# Add only intentional overrides here. Packaged defaults load automatically.
# Do not copy the packaged ruleset: that would hide future default-rule updates.
rules: []
"""


def _seed_default(
    destination: Path, content: bytes, source: Path | None = None
) -> bool:
    """Create a missing default, or explain when a preserved file differs."""
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if destination.read_bytes() != content:
            if source is not None:
                print(
                    f"Preserved {destination}: differs from the bundled default at "
                    f"{source}. Compare these files to incorporate updates."
                )
            else:
                print(
                    f"Preserved {destination}: user rules override packaged defaults. "
                    "Keep intentional overrides; remove copied default rules to "
                    "inherit future default-rule updates."
                )
        return False
    with os.fdopen(fd, "wb") as output:
        output.write(content)
    return True


def seed_user_data() -> int:
    """Copy missing defaults only; never replace user edits or credentials."""
    home = Path.home() / ".kerkov"
    prepare_private_parent(home, managed_parent=home)
    sources = []
    for directory in ("skills", "agents", "config"):
        root = bundled_asset_path(directory)
        for source in sorted(root.rglob("*")):
            if source.is_file() and source.suffix in (".md", ".py", ".yaml"):
                # Only the HTTP defaults belong in the seeded config folder.
                if directory == "config" and source.name != "http_profiles.yaml":
                    continue
                sources.append((source, home / directory / source.relative_to(root)))

    copied = 0
    for source, destination in sources:
        copied += _seed_default(destination, source.read_bytes(), source)
    # This is an override layer, not a snapshot of the shipped ruleset.
    copied += _seed_default(home / "approvals.yaml", USER_APPROVALS_TEMPLATE)
    return copied


def configure_path(bin_dir: Path) -> None:
    """Prepend our bin directory in supported shells and GitHub Actions."""
    raw_path = str(bin_dir)
    if any(char in raw_path for char in "\r\n\0"):
        raise ValueError("Install directory must not contain control characters")
    quoted = shlex.quote(raw_path)
    line = f'export PATH={quoted}:"$PATH" # Kerkov installer'
    home = Path.home()
    login_file = next(
        (
            home / name
            for name in (".bash_profile", ".bash_login", ".profile")
            if (home / name).exists()
        ),
        home / ".profile",
    )
    zsh_home = Path(os.environ.get("ZDOTDIR") or home)
    fish_home = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    # Fish single quotes only escape backslashes and single quotes.
    fish_path = "'" + raw_path.replace("\\", "\\\\").replace("'", "\\'") + "'"
    targets = {
        home / ".bashrc": line,
        login_file: line,
        home / ".profile": line,  # POSIX sh / ash login shells
        zsh_home / ".zshrc": line,
        fish_home
        / "fish"
        / "conf.d"
        / "kerkov.fish": f"set -gx PATH {fish_path} $PATH # Kerkov installer",
    }
    for path, entry in targets.items():
        try:
            # Profiles may use a legacy encoding. Probe and append bytes so
            # their existing contents are neither decoded nor rewritten.
            existing = path.read_bytes() if path.exists() else b""
            encoded_entry = entry.encode("utf-8")
            if encoded_entry in existing.splitlines():
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("ab") as output:
                output.write(b"\n" + encoded_entry + b"\n")
        except OSError as exc:
            print(f"Could not update {path}: {exc}. Add this manually: {entry}")
    if os.environ.get("GITHUB_PATH"):
        with Path(os.environ["GITHUB_PATH"]).open("a") as output:
            output.write(raw_path + "\n")


def chromium_available() -> bool:
    """Check a real headless launch, including required OS libraries, with a limit."""
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from playwright.sync_api import sync_playwright\n"
                    "with sync_playwright() as p:\n"
                    "    browser = p.chromium.launch()\n"
                    "    browser.close()\n"
                ),
            ],
            capture_output=True,
            timeout=20,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def preflight() -> None:
    """Informational checks; no credential access or downloads."""
    for tool in ("nmap", "docker", "opengrep", "syft", "grype"):
        print(f"Optional {tool}: {shutil.which(tool) or 'not on PATH'}")
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import keyring; "
                    "raise SystemExit(0 if keyring.get_keyring().priority > 0 else 1)"
                ),
            ],
            capture_output=True,
            timeout=5,
        )
        keyring_detected = result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        keyring_detected = False
    if keyring_detected:
        print("OS keyring backend detected; first-run prompts verify access.")
    else:
        print(
            "No usable OS keyring detected. Keys can be saved in "
            "~/.kerkov/credentials.json (0600). Use /auth to manage credentials."
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", action="store_true")
    parser.add_argument("--path", type=Path)
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--check-chromium", action="store_true")
    args = parser.parse_args()
    exit_code = 0
    for option, requested in (("--seed", args.seed), ("--path", args.path)):
        if not requested:
            continue
        try:
            if option == "--seed":
                print(f"Seeded {seed_user_data()} missing defaults under ~/.kerkov.")
            else:
                configure_path(args.path)
        except (OSError, ValueError) as exc:
            command = [sys.executable, "-m", "kerkov.core.install_support", option]
            if option == "--path":
                command.append(str(args.path))
            print(
                f"Could not finish {option} setup: {exc}. Check directory ownership "
                f"and permissions, then retry: {shlex.join(command)}",
                file=sys.stderr,
            )
            exit_code = 1
    if args.preflight:
        preflight()
    if args.check_chromium and not chromium_available():
        exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
