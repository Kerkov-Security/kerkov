"""Run the real shell installer against isolated command stubs; never download."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "install.sh"
STUB = r"""
import json, os, pathlib, shutil, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
env = os.environ
with open(env["EVENTS"], "a") as out:
    out.write(json.dumps([name, *args]) + "\n")
if name == "uname":
    print(env.get("TEST_OS", "Linux") if args == ["-s"] else env.get("TEST_ARCH", "x86_64"))
elif name == "curl":
    if "astral.sh" in args[-1]:
        print('mkdir -p "$UV_INSTALL_DIR"; cp "$FAKE_UV" "$UV_INSTALL_DIR/uv"')
    else:
        if env.get("FAIL_RELEASE"):
            sys.exit(22)
        print(json.dumps({"tag_name": env.get("TAG", "v0.1.0")}))
elif name in ("uv", "fake-uv"):
    if args[:2] == ["tool", "dir"]:
        print(env["UV_TOOL_DIR"])
    else:
        if env.get("FAIL_INSTALL"):
            sys.exit(1)
        assert args[:2] == ["tool", "install"], args
        bindir = pathlib.Path(env["UV_TOOL_BIN_DIR"])
        bindir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(env["STUB"], bindir / "kerkov")
        (bindir / "kerkov").chmod(0o755)
        python = pathlib.Path(env["UV_TOOL_DIR"]) / "kerkov/bin/python"
        python.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(env["STUB"], python)
        python.chmod(0o755)
elif name == "kerkov":
    print("kerkov " + env.get("INSTALLED_VERSION", "0.1.0"))
elif name == "python":
    if "--seed" in args and env.get("FAIL_SEED"):
        sys.exit(1)
    if "--path" in args and env.get("FAIL_PATH"):
        sys.exit(1)
    if "--check-chromium" in args:
        sys.exit(1)
    if args[:3] == ["-m", "playwright", "install"] and env.get("FAIL_CHROMIUM"):
        sys.exit(1)
else:
    raise AssertionError((name, args))
"""


@pytest.fixture
def installer(tmp_path):
    home = tmp_path / "home with spaces"
    home.mkdir()
    commands = tmp_path / "commands"
    commands.mkdir()
    stub = tmp_path / "stub"
    stub.write_text(f"#!{sys.executable}\n" + STUB)
    stub.chmod(0o755)
    fake_uv = tmp_path / "fake-uv"
    shutil.copy(stub, fake_uv)
    for name in ("uname", "curl", "uv"):
        shutil.copy(stub, commands / name)
    events = tmp_path / "events"
    env = {
        "HOME": str(home),
        "PATH": f"{commands}:/usr/bin:/bin",
        "CI": "true",
        "STUB": str(stub),
        "FAKE_UV": str(fake_uv),
        "EVENTS": str(events),
        "UV_TOOL_DIR": str(tmp_path / "uv-tools"),
    }

    def run(*args, script_text=None, **updates):
        result = subprocess.run(
            ["bash", "-s", "--", *args],
            input=SCRIPT.read_text() if script_text is None else script_text,
            cwd=tmp_path,
            env={**env, **updates},
            text=True,
            capture_output=True,
            timeout=15,
        )
        calls = (
            [json.loads(line) for line in events.read_text().splitlines()]
            if events.exists()
            else []
        )
        events.write_text("")
        return result, calls

    run.commands = commands
    run.home = home
    return run


def test_latest_release_install_and_default_no_download(installer):
    result, calls = installer()
    assert result.returncode == 0, result.stderr
    install = next(call for call in calls if call[:3] == ["uv", "tool", "install"])
    assert (
        install[-1]
        == "kerkov @ https://github.com/Kerkov-Security/kerkov/releases/download/v0.1.0/kerkov-0.1.0-py3-none-any.whl"
    )
    assert install[install.index("--python") + 1] == "3.13"
    assert any("--seed" in call for call in calls)
    assert any("--path" in call for call in calls)
    assert not any(
        call[:4] == ["python", "-m", "playwright", "install"] for call in calls
    )
    assert "/auth" in result.stdout
    assert "export LLM_API_KEY" not in result.stdout


def test_pinned_web_and_explicit_chromium(installer):
    result, calls = installer("--web", "--chromium", VERSION="v0.1.0")
    assert result.returncode == 0, result.stderr
    assert not any(call[0] == "curl" for call in calls)
    assert any("kerkov[web] @ " in call[-1] for call in calls)
    assert ["python", "-m", "playwright", "install", "chromium"] in calls


@pytest.mark.parametrize(
    "url",
    [
        "file:///inputs/dist/kerkov-0.1.0-py3-none-any.whl",
        "https://example.test/kerkov-0.1.0-py3-none-any.whl",
    ],
)
def test_built_wheel_override_skips_release_lookup(installer, url):
    result, calls = installer(VERSION="0.1.0", KERKOV_WHEEL_URL=url)
    assert result.returncode == 0, result.stderr
    assert not any(call[0] == "curl" for call in calls)
    install = next(call for call in calls if call[:3] == ["uv", "tool", "install"])
    assert install[-1] == f"kerkov @ {url}"


def test_built_wheel_override_requires_explicit_version(installer):
    result, calls = installer(KERKOV_WHEEL_URL="file:///inputs/kerkov.whl")
    assert result.returncode != 0
    assert "VERSION is required" in result.stderr
    assert not any(call[0] in ("uv", "curl") for call in calls)


@pytest.mark.parametrize("url", ["http://example.test/kerkov.whl", "/tmp/kerkov.whl"])
def test_built_wheel_override_rejects_unsupported_urls(installer, url):
    result, calls = installer(VERSION="0.1.0", KERKOV_WHEEL_URL=url)
    assert result.returncode != 0
    assert "HTTPS or absolute file URL" in result.stderr
    assert not any(call[0] in ("uv", "curl") for call in calls)


def test_bootstrap_uv_is_visible_and_has_no_profile_side_effects(installer):
    (installer.commands / "uv").unlink()
    result, calls = installer(VERSION="0.1.0")
    assert result.returncode == 0, result.stderr
    assert "Installing uv" in result.stdout
    assert any(call[-1] == "https://astral.sh/uv/install.sh" for call in calls)


@pytest.mark.parametrize(
    "version", ["main", "../bad", "0.1", "0.1.0;echo bad", "01.0.0"]
)
def test_rejects_invalid_version_before_installing(installer, version):
    result, calls = installer(VERSION=version)
    assert result.returncode != 0
    assert "Invalid release version" in result.stderr
    assert not any(call[0] == "uv" for call in calls)


@pytest.mark.parametrize(
    "system,arch", [("FreeBSD", "x86_64"), ("Linux", "i686"), ("MINGW64_NT", "x86_64")]
)
def test_unsupported_platform_fails_with_guidance(installer, system, arch):
    result, calls = installer(TEST_OS=system, TEST_ARCH=arch)
    assert result.returncode != 0
    assert not any(call[0] in ("curl", "uv") for call in calls)
    assert "supported" in result.stderr.lower() or "install.ps1" in result.stderr


@pytest.mark.parametrize(
    "system,arch", [("Darwin", "arm64"), ("Darwin", "x86_64"), ("Linux", "aarch64")]
)
def test_supported_platforms(installer, system, arch):
    result, _ = installer(TEST_OS=system, TEST_ARCH=arch)
    assert result.returncode == 0, result.stderr


def test_release_failure_does_not_bootstrap_uv(installer):
    result, calls = installer(FAIL_RELEASE="1")
    assert result.returncode != 0
    assert "first release" in result.stderr
    assert not any(call[0] == "uv" for call in calls)


def test_verification_failure_stops_before_setup(installer):
    result, calls = installer(INSTALLED_VERSION="0.0.9")
    assert result.returncode != 0
    assert "Verification failed" in result.stderr
    assert not any("--seed" in call for call in calls)


def test_chromium_failure_is_nonfatal(installer):
    result, _ = installer("--chromium", FAIL_CHROMIUM="1")
    assert result.returncode == 0
    assert "Chromium download failed" in result.stderr


@pytest.mark.parametrize("failure", ["FAIL_SEED", "FAIL_PATH"])
def test_setup_failure_continues_to_preflight_and_next_steps(installer, failure):
    result, calls = installer(**{failure: "1"})
    assert result.returncode == 0, result.stderr
    assert "setup is incomplete" in result.stderr
    assert any("--path" in call for call in calls)
    assert any("--preflight" in call for call in calls)
    assert "export PATH=" in result.stdout
    assert "/auth" in result.stdout


def test_no_modify_path_still_supports_github_actions(installer, tmp_path):
    github_path = tmp_path / "github-path"
    result, calls = installer("--no-modify-path", GITHUB_PATH=str(github_path))
    assert result.returncode == 0
    assert not any("--path" in call for call in calls)
    assert github_path.read_text().strip() == str(installer.home / ".kerkov/bin")


def test_old_install_is_reported_and_preserved(installer):
    old = installer.commands / "kerkov"
    old.write_text("old executable")
    old.chmod(0o755)
    result, _ = installer()
    assert result.returncode == 0
    assert "takes precedence" in result.stdout
    assert old.read_text() == "old executable"


def test_truncated_download_does_not_install(installer):
    result, calls = installer(
        script_text=SCRIPT.read_text().split("    local uv_bin")[0]
    )
    assert result.returncode != 0
    assert calls == []


def test_uv_failure_does_not_print_success(installer):
    result, calls = installer(FAIL_INSTALL="1")
    assert result.returncode != 0
    assert "Installation failed" in result.stderr
    assert not any("--seed" in call for call in calls)


@pytest.mark.parametrize(
    "directory", ["relative/bin", "/tmp/has:colon", "/tmp/has\nnewline"]
)
def test_invalid_install_directory_is_rejected(installer, directory):
    result, calls = installer(INSTALL_DIR=directory)
    assert result.returncode != 0
    assert not any(call[0] == "uv" for call in calls)
