"""Tests for Kerkov's latest-stable OpenGrep installer."""

import asyncio
import hashlib
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, List
from unittest.mock import patch

from kerkov.core.opengrep_runtime import (
    LATEST_RELEASE_API,
    OpenGrepInstaller,
    OpenGrepReleaseAsset,
    OpenGrepRuntimeError,
    _latest_release_asset,
    current_platform_key,
)

_PLATFORM_KEY = "linux-x86_64-glibc"


def _asset(data: bytes, *, version: str = "v9.9.9") -> OpenGrepReleaseAsset:
    return OpenGrepReleaseAsset(
        version=version,
        filename="opengrep_manylinux_x86",
        url=(
            "https://github.com/opengrep/opengrep/releases/download/"
            f"{version}/opengrep_manylinux_x86"
        ),
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self, size: int = -1) -> bytes:
        return self.payload if size < 0 else self.payload[:size]

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None


@contextmanager
def _patched_release(payload: Any) -> Iterator[List[Any]]:
    requests: List[Any] = []

    def open_request(request: Any, timeout: float) -> _Response:
        requests.append((request, timeout))
        return _Response(json.dumps(payload).encode("utf-8"))

    with patch("kerkov.core.opengrep_runtime.urlopen", side_effect=open_request):
        yield requests


class LatestReleaseMetadataTest(unittest.TestCase):
    def _release(self, **asset_overrides: object) -> dict[str, Any]:
        asset = {
            "name": "opengrep_manylinux_x86",
            "size": 1234,
            "digest": "sha256:" + "a" * 64,
            "browser_download_url": (
                "https://github.com/opengrep/opengrep/releases/download/"
                "v9.9.9/opengrep_manylinux_x86"
            ),
        }
        asset.update(asset_overrides)
        return {
            "tag_name": "v9.9.9",
            "draft": False,
            "prerelease": False,
            "assets": [asset],
        }

    def test_selects_latest_stable_platform_asset_and_github_digest(self) -> None:
        with _patched_release(self._release()) as requests:
            asset = _latest_release_asset(_PLATFORM_KEY)

        self.assertEqual(asset.version, "v9.9.9")
        self.assertEqual(asset.filename, "opengrep_manylinux_x86")
        self.assertEqual(asset.size, 1234)
        self.assertEqual(asset.sha256, "a" * 64)
        request, timeout = requests[0]
        self.assertEqual(request.full_url, LATEST_RELEASE_API)
        self.assertEqual(request.get_header("Accept"), "application/vnd.github+json")
        self.assertEqual(timeout, 30)

    def test_rejects_prerelease_metadata(self) -> None:
        release = self._release()
        release["prerelease"] = True

        with (
            _patched_release(release),
            self.assertRaisesRegex(OpenGrepRuntimeError, "non-stable"),
        ):
            _latest_release_asset(_PLATFORM_KEY)

    def test_rejects_invalid_release_version(self) -> None:
        release = self._release()
        release["tag_name"] = "v9.9.9\nforged-log-entry"

        with (
            _patched_release(release),
            self.assertRaisesRegex(OpenGrepRuntimeError, "version"),
        ):
            _latest_release_asset(_PLATFORM_KEY)

    def test_requires_github_published_sha256(self) -> None:
        for digest in [None, "sha512:" + "a" * 64, "sha256:not-a-digest"]:
            with (
                self.subTest(digest=digest),
                _patched_release(self._release(digest=digest)),
                self.assertRaisesRegex(OpenGrepRuntimeError, "SHA-256"),
            ):
                _latest_release_asset(_PLATFORM_KEY)

    def test_rejects_non_official_download_url(self) -> None:
        release = self._release(browser_download_url="https://example.test/opengrep")

        with (
            _patched_release(release),
            self.assertRaisesRegex(OpenGrepRuntimeError, "URL"),
        ):
            _latest_release_asset(_PLATFORM_KEY)

    def test_requires_an_asset_for_the_current_platform(self) -> None:
        release = self._release(name="opengrep_osx_x86")

        with (
            _patched_release(release),
            self.assertRaisesRegex(OpenGrepRuntimeError, _PLATFORM_KEY),
        ):
            _latest_release_asset(_PLATFORM_KEY)


class OpenGrepInstallerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _installer(
        self,
        data: bytes,
        *,
        downloader: object,
        locator: object = lambda _: None,
        probe: object = lambda _: True,
        platform_key: str = _PLATFORM_KEY,
        release_loader: object = None,
    ) -> OpenGrepInstaller:
        loader = release_loader or (lambda _: _asset(data))
        return OpenGrepInstaller(
            install_root=self.root / "install",
            platform_key=platform_key,
            release_loader=loader,  # type: ignore[arg-type]
            downloader=downloader,  # type: ignore[arg-type]
            locator=locator,  # type: ignore[arg-type]
            probe=probe,  # type: ignore[arg-type]
        )

    def test_uses_a_working_system_binary_without_release_lookup(self) -> None:
        system_binary = self.root / "bin" / "opengrep"
        system_binary.parent.mkdir()
        system_binary.write_bytes(b"system")

        def unexpected(*args: object) -> None:
            self.fail(f"unexpected installer call: {args}")

        installer = self._installer(
            b"download",
            downloader=unexpected,
            locator=lambda _: str(system_binary),
            probe=lambda path: path == system_binary,
            release_loader=unexpected,
        )

        info = asyncio.run(installer.resolve(install_if_missing=True))

        self.assertEqual(info.source, "system")
        self.assertEqual(Path(info.binary), system_binary)
        self.assertFalse(info.installed)

    def test_permission_denied_system_candidate_installs_latest(self) -> None:
        data = b"verified latest binary"
        system_binary = self.root / "bin" / "opengrep"
        system_binary.parent.mkdir()
        system_binary.write_bytes(b"not runnable")
        downloads: List[OpenGrepReleaseAsset] = []

        def download(
            asset: OpenGrepReleaseAsset, destination: Path, timeout: float
        ) -> None:
            downloads.append(asset)
            self.assertGreater(timeout, 0)
            destination.write_bytes(data)

        installer = self._installer(
            data,
            downloader=download,
            locator=lambda _: str(system_binary),
            probe=lambda path: path != system_binary,
        )

        info = asyncio.run(installer.resolve(install_if_missing=True))

        self.assertEqual(info.source, "kerkov_install")
        self.assertEqual(info.version, "v9.9.9")
        self.assertTrue(info.installed)
        self.assertEqual(Path(info.binary).read_bytes(), data)
        self.assertEqual([asset.version for asset in downloads], ["v9.9.9"])
        if os.name != "nt":
            self.assertTrue(os.access(info.binary, os.X_OK))

    def test_existing_kerkov_install_is_reused_without_update_check(self) -> None:
        installed = self.root / "install" / "opengrep"
        installed.parent.mkdir()
        installed.write_bytes(b"existing")

        def unexpected(*args: object) -> None:
            self.fail(f"unexpected update check: {args}")

        installer = self._installer(
            b"download",
            downloader=unexpected,
            release_loader=unexpected,
            probe=lambda path: path == installed,
        )

        info = asyncio.run(installer.resolve(install_if_missing=True))

        self.assertEqual(info.source, "kerkov_install")
        self.assertFalse(info.installed)
        self.assertIsNone(info.version)

    def test_concurrent_resolves_install_only_once(self) -> None:
        data = b"concurrent binary"
        download_count = 0

        def download(
            _asset: OpenGrepReleaseAsset, destination: Path, _timeout: float
        ) -> None:
            nonlocal download_count
            download_count += 1
            destination.write_bytes(data)

        installer = self._installer(data, downloader=download)

        async def resolve_both() -> object:
            return await asyncio.gather(
                installer.resolve(install_if_missing=True),
                installer.resolve(install_if_missing=True),
            )

        results = asyncio.run(resolve_both())  # type: ignore[assignment]

        self.assertEqual(download_count, 1)
        self.assertEqual([result.installed for result in results], [True, False])

    def test_install_can_be_disabled(self) -> None:
        installer = self._installer(
            b"download",
            downloader=lambda *_: self.fail("download should be disabled"),
            release_loader=lambda *_: self.fail("lookup should be disabled"),
        )

        with self.assertRaisesRegex(OpenGrepRuntimeError, "install_if_missing=true"):
            asyncio.run(installer.resolve(install_if_missing=False))

    def test_checksum_mismatch_is_rejected_and_not_installed(self) -> None:
        trusted = b"trusted binary"
        untrusted = b"tamperd binary"
        self.assertEqual(len(trusted), len(untrusted))

        def download(
            _asset: OpenGrepReleaseAsset, destination: Path, _timeout: float
        ) -> None:
            destination.write_bytes(untrusted)

        installer = self._installer(trusted, downloader=download)

        with self.assertRaisesRegex(OpenGrepRuntimeError, "SHA-256"):
            asyncio.run(installer.resolve(install_if_missing=True))

        self.assertFalse((self.root / "install" / "opengrep").exists())
        self.assertEqual(list((self.root / "install").glob(".opengrep-download-*")), [])

    def test_unsupported_platform_is_actionable(self) -> None:
        installer = self._installer(
            b"download",
            platform_key="plan9-mips",
            downloader=lambda *_: self.fail("unsupported platforms cannot download"),
            release_loader=_latest_release_asset,
        )

        with self.assertRaisesRegex(OpenGrepRuntimeError, "plan9-mips"):
            asyncio.run(installer.resolve(install_if_missing=True))

    def test_refuses_symlinked_install_directory(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        install_root = self.root / "install"
        try:
            install_root.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlinks are unavailable: {exc}")
        data = b"download"
        installer = OpenGrepInstaller(
            install_root=install_root,
            platform_key=_PLATFORM_KEY,
            release_loader=lambda _: _asset(data),
            downloader=lambda asset, path, timeout: path.write_bytes(data),
            locator=lambda _: None,
            probe=lambda _: True,
        )

        with self.assertRaisesRegex(OpenGrepRuntimeError, "symlink"):
            asyncio.run(installer.resolve(install_if_missing=True))

        self.assertEqual(list(outside.iterdir()), [])


class CurrentPlatformKeyTest(unittest.TestCase):
    def test_detects_wsl_and_other_glibc_linux_as_manylinux(self) -> None:
        with (
            patch("kerkov.core.opengrep_runtime.platform.system", return_value="Linux"),
            patch(
                "kerkov.core.opengrep_runtime.platform.machine",
                return_value="x86_64",
            ),
            patch(
                "kerkov.core.opengrep_runtime.platform.libc_ver",
                return_value=("glibc", "2.40"),
            ),
        ):
            self.assertEqual(current_platform_key(), "linux-x86_64-glibc")

    def test_normalizes_supported_desktop_architectures(self) -> None:
        cases = [
            ("Darwin", "arm64", "darwin-arm64"),
            ("Darwin", "AMD64", "darwin-x86_64"),
            ("Windows", "AMD64", "windows-x86_64"),
        ]
        for system, machine, expected in cases:
            with (
                self.subTest(system=system, machine=machine),
                patch(
                    "kerkov.core.opengrep_runtime.platform.system",
                    return_value=system,
                ),
                patch(
                    "kerkov.core.opengrep_runtime.platform.machine",
                    return_value=machine,
                ),
            ):
                self.assertEqual(current_platform_key(), expected)


if __name__ == "__main__":
    unittest.main()
