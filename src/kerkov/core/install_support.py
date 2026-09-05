"""Post-install setup, run with the Python interpreter in the tool environment."""

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path

from kerkov.core.private_files import prepare_private_parent
from kerkov.core.project_paths import bundled_asset_path


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

    sources.append(
        (
            resources.files("kerkov.config").joinpath("approvals.yaml"),
            home / "approvals.yaml",
        )
    )
    copied = 0
    for source, destination in sources:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as output:
            output.write(source.read_bytes())
        copied += 1
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
            existing = path.read_text() if path.exists() else ""
            if entry in existing.splitlines():
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as output:
                output.write(f"\n{entry}\n")
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", action="store_true")
    parser.add_argument("--path", type=Path)
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--check-chromium", action="store_true")
    args = parser.parse_args()
    if args.seed:
        print(f"Seeded {seed_user_data()} missing defaults under ~/.kerkov.")
    if args.path:
        configure_path(args.path)
    if args.preflight:
        preflight()
    if args.check_chromium:
        raise SystemExit(0 if chromium_available() else 1)


if __name__ == "__main__":
    main()
