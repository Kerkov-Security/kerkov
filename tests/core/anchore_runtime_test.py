"""Tests for Kerkov's verified Syft and Grype runtime installer."""

import asyncio
import hashlib
import io
import json
import os
import tarfile
import tempfile
import time
import unittest
import zipfile
from contextlib import contextmanager
from http.client import IncompleteRead
from pathlib import Path
from typing import Any, Iterator, List
from unittest.mock import patch

from kerkov.core.anchore_runtime import (
    AnchoreInstaller,
    AnchoreReleaseAsset,
    AnchoreRuntimeError,
    _archive_name,
    _download_asset,
    _download_small,
    _latest_release_asset,
    current_platform_key,
)

_PLATFORM_KEY = "linux-amd64"


def _tar_archive(tool: str, data: bytes) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        info = tarfile.TarInfo(name=tool)
        info.mode = 0o755
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


def _zip_archive(tool: str, data: bytes) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w") as archive:
        archive.writestr(f"{tool}.exe", data)
    return output.getvalue()


def _asset(
    tool: str,
    archive: bytes,
    *,
    version: str = "v9.9.9",
    platform_key: str = _PLATFORM_KEY,
) -> AnchoreReleaseAsset:
    filename = _archive_name(tool, version, platform_key)
    return AnchoreReleaseAsset(
        tool=tool,
        version=version,
        filename=filename,
        url=(
            f"https://github.com/anchore/{tool}/releases/download/"
            f"{version}/{filename}"
        ),
        size=len(archive),
        sha256=hashlib.sha256(archive).hexdigest(),
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


class _IncompleteResponse(_Response):
    def read(self, size: int = -1) -> bytes:
        raise IncompleteRead(b"partial", 100)


@contextmanager
def _patched_release(
    release: Any, checksums: bytes
) -> Iterator[List[tuple[str, float]]]:
    requests: List[tuple[str, float]] = []

    def open_request(request: Any, timeout: float) -> _Response:
        requests.append((request.full_url, timeout))
        if request.full_url.endswith("checksums.txt"):
            return _Response(checksums)
        return _Response(json.dumps(release).encode("utf-8"))

    with patch("kerkov.core.anchore_runtime.urlopen", side_effect=open_request):
        yield requests


class LatestReleaseMetadataTest(unittest.TestCase):
    def _release(self, tool: str = "syft") -> tuple[dict[str, Any], bytes]:
        version = "v9.9.9"
        archive_name = f"{tool}_9.9.9_linux_amd64.tar.gz"
        checksum_name = f"{tool}_9.9.9_checksums.txt"
        checksums = f"{'a' * 64}  {archive_name}\n".encode("utf-8")
        release = {
            "tag_name": version,
            "draft": False,
            "prerelease": False,
            "assets": [
                {
                    "name": archive_name,
                    "size": 1234,
                    "digest": "sha256:" + "a" * 64,
                    "browser_download_url": (
                        f"https://github.com/anchore/{tool}/releases/download/"
                        f"{version}/{archive_name}"
                    ),
                },
                {
                    "name": checksum_name,
                    "size": len(checksums),
                    "browser_download_url": (
                        f"https://github.com/anchore/{tool}/releases/download/"
                        f"{version}/{checksum_name}"
                    ),
                },
            ],
        }
        return release, checksums

    def test_selects_archive_and_checksum_from_latest_stable_release(self) -> None:
        release, checksums = self._release()

        with _patched_release(release, checksums) as requests:
            asset = _latest_release_asset("syft", _PLATFORM_KEY)

        self.assertEqual(asset.filename, "syft_9.9.9_linux_amd64.tar.gz")
        self.assertEqual(asset.sha256, "a" * 64)
        self.assertEqual(len(requests), 2)
        self.assertEqual(
            requests[0][0],
            "https://api.github.com/repos/anchore/syft/releases/latest",
        )

    def test_rejects_non_stable_release(self) -> None:
        release, checksums = self._release("grype")
        release["prerelease"] = True

        with (
            _patched_release(release, checksums),
            self.assertRaisesRegex(AnchoreRuntimeError, "non-stable"),
        ):
            _latest_release_asset("grype", _PLATFORM_KEY)

    def test_rejects_untrusted_archive_url(self) -> None:
        release, checksums = self._release()
        release["assets"][0]["browser_download_url"] = "https://example.test/syft"

        with (
            _patched_release(release, checksums),
            self.assertRaisesRegex(AnchoreRuntimeError, "URL"),
        ):
            _latest_release_asset("syft", _PLATFORM_KEY)

    def test_requires_checksum_for_selected_archive(self) -> None:
        release, checksums = self._release()
        wrong = checksums.replace(b"syft_9.9.9", b"other_9.9.9")
        release["assets"][1]["size"] = len(wrong)

        with (
            _patched_release(release, wrong),
            self.assertRaisesRegex(AnchoreRuntimeError, "SHA-256"),
        ):
            _latest_release_asset("syft", _PLATFORM_KEY)

    def test_requires_github_digest_to_match_checksum_file(self) -> None:
        release, checksums = self._release()
        release["assets"][0]["digest"] = "sha256:" + "b" * 64

        with (
            _patched_release(release, checksums),
            self.assertRaisesRegex(AnchoreRuntimeError, "did not match"),
        ):
            _latest_release_asset("syft", _PLATFORM_KEY)

    def test_wraps_truncated_release_metadata_response(self) -> None:
        with (
            patch(
                "kerkov.core.anchore_runtime.urlopen",
                return_value=_IncompleteResponse(b""),
            ),
            self.assertRaisesRegex(AnchoreRuntimeError, "could not read syft"),
        ):
            _latest_release_asset("syft", _PLATFORM_KEY)

    def test_wraps_truncated_archive_response(self) -> None:
        archive = _tar_archive("syft", b"binary")
        asset = _asset("syft", archive)
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "syft.tar.gz"
            with (
                patch(
                    "kerkov.core.anchore_runtime.urlopen",
                    return_value=_IncompleteResponse(b""),
                ),
                self.assertRaisesRegex(AnchoreRuntimeError, "could not download syft"),
            ):
                _download_asset(asset, destination, 1)

    def test_wraps_truncated_checksum_response(self) -> None:
        with (
            patch(
                "kerkov.core.anchore_runtime.urlopen",
                return_value=_IncompleteResponse(b""),
            ),
            self.assertRaisesRegex(
                AnchoreRuntimeError, "could not download release checksums"
            ),
        ):
            _download_small(
                "https://github.com/anchore/syft/releases/download/v1/checksums.txt",
                expected_size=100,
                max_bytes=100,
            )


class AnchoreInstallerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _installer(
        self,
        tool: str,
        archive: bytes,
        *,
        downloader: object,
        locator: object = lambda _: None,
        probe: object = lambda _: True,
        platform_key: str = _PLATFORM_KEY,
        release_loader: object = None,
    ) -> AnchoreInstaller:
        loader = release_loader or (
            lambda selected_tool, selected_platform: _asset(
                selected_tool,
                archive,
                platform_key=selected_platform,
            )
        )
        return AnchoreInstaller(
            tool,
            install_root=self.root / tool,
            platform_key=platform_key,
            release_loader=loader,  # type: ignore[arg-type]
            downloader=downloader,  # type: ignore[arg-type]
            locator=locator,  # type: ignore[arg-type]
            probe=probe,  # type: ignore[arg-type]
        )

    def test_uses_working_system_binary_without_release_lookup(self) -> None:
        system_binary = self.root / "bin" / "syft"
        system_binary.parent.mkdir()
        system_binary.write_bytes(b"system")

        def unexpected(*args: object) -> None:
            self.fail(f"unexpected installer call: {args}")

        installer = self._installer(
            "syft",
            b"unused",
            downloader=unexpected,
            locator=lambda _: str(system_binary),
            probe=lambda path: path == system_binary,
            release_loader=unexpected,
        )

        info = asyncio.run(installer.resolve(install_if_missing=True))

        self.assertEqual(info.source, "system")
        self.assertEqual(Path(info.binary), system_binary)
        self.assertFalse(info.installed)

    def test_installs_verified_tar_binary_and_reuses_it(self) -> None:
        binary = b"verified syft binary"
        archive = _tar_archive("syft", binary)
        downloads = 0

        def download(
            _asset: AnchoreReleaseAsset, destination: Path, _timeout: float
        ) -> None:
            nonlocal downloads
            downloads += 1
            destination.write_bytes(archive)

        installer = self._installer("syft", archive, downloader=download)

        first = asyncio.run(installer.resolve(install_if_missing=True))
        second = asyncio.run(installer.resolve(install_if_missing=True))

        self.assertTrue(first.installed)
        self.assertFalse(second.installed)
        self.assertEqual(Path(first.binary).read_bytes(), binary)
        self.assertEqual(downloads, 1)
        if os.name != "nt":
            self.assertTrue(os.access(first.binary, os.X_OK))

    def test_force_install_replaces_a_working_managed_binary(self) -> None:
        old_binary = self.root / "syft" / "syft"
        old_binary.parent.mkdir()
        old_binary.write_bytes(b"old")
        archive = _tar_archive("syft", b"latest")
        downloads = 0

        def download(
            _asset: AnchoreReleaseAsset, destination: Path, _timeout: float
        ) -> None:
            nonlocal downloads
            downloads += 1
            destination.write_bytes(archive)

        installer = self._installer("syft", archive, downloader=download)

        info = asyncio.run(
            installer.resolve(install_if_missing=True, force_install=True)
        )

        self.assertTrue(info.installed)
        self.assertEqual(old_binary.read_bytes(), b"latest")
        self.assertEqual(downloads, 1)

    def test_installer_can_be_contended_across_event_loops(self) -> None:
        archive = _tar_archive("grype", b"binary")

        def slow_probe(_path: Path) -> bool:
            time.sleep(0.02)
            return True

        installer = self._installer(
            "grype",
            archive,
            downloader=lambda _asset, destination, _timeout: destination.write_bytes(
                archive
            ),
            probe=slow_probe,
        )

        async def resolve_twice() -> None:
            await asyncio.gather(
                installer.resolve(install_if_missing=True),
                installer.resolve(install_if_missing=True),
            )

        asyncio.run(resolve_twice())
        asyncio.run(resolve_twice())

    def test_installs_verified_windows_zip_binary(self) -> None:
        binary = b"verified grype binary"
        archive = _zip_archive("grype", binary)

        def download(
            _asset: AnchoreReleaseAsset, destination: Path, _timeout: float
        ) -> None:
            destination.write_bytes(archive)

        installer = self._installer(
            "grype",
            archive,
            downloader=download,
            platform_key="windows-amd64",
        )

        info = asyncio.run(installer.resolve(install_if_missing=True))

        self.assertEqual(Path(info.binary).name, "grype.exe")
        self.assertEqual(Path(info.binary).read_bytes(), binary)

    def test_checksum_mismatch_is_rejected_without_install(self) -> None:
        trusted = _tar_archive("grype", b"trusted")
        tampered = bytearray(trusted)
        tampered[-10] ^= 1
        untrusted = bytes(tampered)

        def download(
            _asset: AnchoreReleaseAsset, destination: Path, _timeout: float
        ) -> None:
            destination.write_bytes(untrusted)

        installer = self._installer("grype", trusted, downloader=download)

        with self.assertRaisesRegex(AnchoreRuntimeError, "SHA-256"):
            asyncio.run(installer.resolve(install_if_missing=True))

        self.assertFalse((self.root / "grype" / "grype").exists())
        self.assertEqual(list((self.root / "grype").glob(".*-download-*")), [])
        self.assertEqual(list((self.root / "grype").glob(".*-binary-*")), [])

    def test_install_can_be_disabled(self) -> None:
        installer = self._installer(
            "syft",
            b"unused",
            downloader=lambda *_: self.fail("download should be disabled"),
            release_loader=lambda *_: self.fail("lookup should be disabled"),
        )

        with self.assertRaisesRegex(AnchoreRuntimeError, "install_if_missing=true"):
            asyncio.run(installer.resolve(install_if_missing=False))

    def test_force_install_requires_install_permission(self) -> None:
        installer = self._installer(
            "syft",
            b"unused",
            downloader=lambda *_: self.fail("download should be disabled"),
            release_loader=lambda *_: self.fail("lookup should be disabled"),
        )

        with self.assertRaisesRegex(AnchoreRuntimeError, "force_install requires"):
            asyncio.run(installer.resolve(install_if_missing=False, force_install=True))

    def test_refuses_symlinked_install_directory(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        install_root = self.root / "syft"
        try:
            install_root.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlinks are unavailable: {exc}")
        archive = _tar_archive("syft", b"binary")
        installer = AnchoreInstaller(
            "syft",
            install_root=install_root,
            platform_key=_PLATFORM_KEY,
            release_loader=lambda tool, platform_key: _asset(tool, archive),
            downloader=lambda asset, path, timeout: path.write_bytes(archive),
            locator=lambda _: None,
            probe=lambda _: True,
        )

        with self.assertRaisesRegex(AnchoreRuntimeError, "symlink"):
            asyncio.run(installer.resolve(install_if_missing=True))

        self.assertEqual(list(outside.iterdir()), [])


class CurrentPlatformKeyTest(unittest.TestCase):
    def test_normalizes_supported_desktop_architectures(self) -> None:
        cases = [
            ("Linux", "x86_64", "linux-amd64"),
            ("Darwin", "AMD64", "darwin-amd64"),
            ("Windows", "arm64", "windows-arm64"),
        ]
        for system, machine, expected in cases:
            with (
                self.subTest(system=system, machine=machine),
                patch(
                    "kerkov.core.anchore_runtime.platform.system",
                    return_value=system,
                ),
                patch(
                    "kerkov.core.anchore_runtime.platform.machine",
                    return_value=machine,
                ),
            ):
                self.assertEqual(current_platform_key(), expected)


if __name__ == "__main__":
    unittest.main()
