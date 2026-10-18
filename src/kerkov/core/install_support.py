"""Post-install setup, run with the Python interpreter in the tool environment."""

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from importlib import resources
from pathlib import Path

from kerkov.core.private_files import prepare_private_parent
from kerkov.core.project_paths import bundled_asset_path

USER_APPROVALS_TEMPLATE = b"""# User approval overrides (first match wins).
# Add only intentional overrides here. Packaged defaults load automatically.
# Do not copy the packaged ruleset: that would hide future default-rule updates.
rules: []
"""
ASSET_MANIFEST = ".bundled-assets.json"


def _write_private_file(destination: Path, content: bytes) -> None:
    """Replace a managed file atomically, with owner-only permissions."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _asset_hashes(path: Path) -> dict[str, str]:
    if path.is_symlink():
        raise PermissionError("Bundled asset manifest must not be a symlink.")
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        if data.get("version") != 1 or not isinstance(data.get("files"), dict):
            raise ValueError("Unrecognized manifest")
        return {
            key: value
            for key, value in data["files"].items()
            if isinstance(value, str) and len(value) == 64
        }
    except (OSError, ValueError, AttributeError):
        print(
            "Could not read bundled asset history; existing differences are preserved.",
            file=sys.stderr,
        )
        return {}


def _legacy_asset_hashes() -> dict[str, list[str]]:
    """Recognize pre-manifest repository defaults without guessing user edits."""
    data = json.loads(
        resources.files("kerkov.config")
        .joinpath("legacy_asset_hashes.json")
        .read_text()
    )
    return data["files"]


def _seed_default(
    destination: Path,
    content: bytes,
    source: Path | None = None,
    *,
    previous_hashes: tuple[str, ...] = (),
    verbose: bool = True,
) -> bool:
    """Create missing defaults and update unchanged defaults; preserve overrides."""
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if destination.is_symlink() or not destination.is_file():
            if verbose:
                print(f"Preserved {destination}: not a regular managed file.")
            return False
        if destination.read_bytes() != content:
            if hashlib.sha256(destination.read_bytes()).hexdigest() in previous_hashes:
                _write_private_file(destination, content)
                return True
            if source is not None and verbose:
                print(
                    f"Preserved {destination}: differs from the bundled default at "
                    f"{source}. Compare these files to incorporate updates."
                )
            elif verbose:
                print(
                    f"Preserved {destination}: user rules override packaged defaults. "
                    "Keep intentional overrides; remove copied default rules to "
                    "inherit future default-rule updates."
                )
        return False
    with os.fdopen(fd, "wb") as output:
        output.write(content)
    return True


def seed_user_data(*, verbose: bool = True) -> int:
    """Refresh shipped defaults while preserving user edits and credentials."""
    home = Path.home() / ".kerkov"
    prepare_private_parent(home, managed_parent=home)
    manifest = home / ASSET_MANIFEST
    legacy = _legacy_asset_hashes() if not manifest.exists() else {}
    previous = _asset_hashes(manifest)
    tracked = dict(previous)
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
        # A user directory link must not redirect installer writes elsewhere.
        if any(
            parent.is_symlink()
            for parent in destination.parents
            if parent != home and home in parent.parents
        ):
            if verbose:
                print(f"Preserved {destination}: parent directory is a symlink.")
            continue
        key = destination.relative_to(home).as_posix()
        hashes = (previous[key],) if key in previous else tuple(legacy.get(key, ()))
        content = source.read_bytes()
        copied += _seed_default(
            destination, content, source, previous_hashes=hashes, verbose=verbose
        )
        if (
            not destination.is_symlink()
            and destination.is_file()
            and destination.read_bytes() == content
        ):
            tracked[key] = hashlib.sha256(content).hexdigest()
    # This is an override layer, not a snapshot of the shipped ruleset.
    copied += _seed_default(
        home / "approvals.yaml", USER_APPROVALS_TEMPLATE, verbose=verbose
    )
    if tracked != previous or not manifest.exists():
        _write_private_file(
            manifest,
            (
                json.dumps({"version": 1, "files": tracked}, indent=2, sort_keys=True)
                + "\n"
            ).encode(),
        )
    return copied


def refresh_installed_defaults() -> None:
    """Apply installed-tool upgrades even when no installer hook was run."""
    if not resources.files("kerkov").joinpath("bundled", "skills").is_dir():
        return  # Source checkouts use their repository assets directly.
    home = Path.home() / ".kerkov"
    if not any((home / name).exists() for name in ("skills", "agents", ASSET_MANIFEST)):
        return  # A direct, unseeded install already uses the current bundle.
    try:
        seed_user_data(verbose=False)
    except (OSError, ValueError) as exc:
        print(
            f"Could not refresh bundled defaults: {exc}. Check ~/.kerkov ownership "
            f"and rerun {shlex.quote(sys.executable)} -m kerkov.core.install_support --seed.",
            file=sys.stderr,
        )


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
                print(f"Seeded or updated {seed_user_data()} defaults under ~/.kerkov.")
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
