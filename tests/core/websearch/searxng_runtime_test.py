"""Tests for the managed SearXNG Docker runtime."""

import asyncio
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional, Sequence
from unittest.mock import patch

import yaml

from kerkov.core.docker_command import CommandResult
from kerkov.core.websearch.searxng_runtime import (
    CONTAINER_SETTINGS_PATH,
    DockerSearxngRuntime,
    SearxngRuntimeError,
    choose_loopback_base_url,
    normalize_searxng_base_url,
    render_settings,
    wait_for_searxng,
)


class FakeRunner:
    def __init__(self, responses: Sequence[CommandResult]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[list[str], float, Optional[bytes]]] = []

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_seconds: float,
        input_data: Optional[bytes] = None,
    ) -> CommandResult:
        self.calls.append((list(args), timeout_seconds, input_data))
        if not self.responses:
            raise AssertionError(f"Unexpected command: {args}")
        return self.responses.pop(0)


def _runtime(runner: FakeRunner, config_dir: Path) -> DockerSearxngRuntime:
    return DockerSearxngRuntime(
        image="docker.io/searxng/searxng:latest",
        container_name="kerkov-searxng",
        config_dir=config_dir,
        runner=runner,
    )


def _daemon_ready() -> CommandResult:
    return CommandResult(exit_code=0, stdout="27.0.1\n")


def _missing_container() -> CommandResult:
    return CommandResult(exit_code=1, stderr="Error: No such object: kerkov-searxng")


def _managed_container(running: bool = True) -> CommandResult:
    return CommandResult(exit_code=0, stdout=f"searxng|{str(running).lower()}\n")


def _call_args(runner: FakeRunner, command: str) -> list[str]:
    return next(
        args
        for args, _, _ in runner.calls
        if len(args) > 1 and args[0] == "docker" and args[1] == command
    )


class SettingsTest(unittest.TestCase):
    def test_enables_the_json_results_format(self) -> None:
        parsed = yaml.safe_load(render_settings("s3cret-key_0"))

        self.assertIn("json", parsed["search"]["formats"])
        self.assertTrue(parsed["use_default_settings"])
        self.assertEqual(parsed["server"]["secret_key"], "s3cret-key_0")
        self.assertFalse(parsed["server"]["limiter"])

    def test_rejects_a_secret_that_would_break_the_yaml(self) -> None:
        with self.assertRaises(ValueError):
            render_settings('" \ninjected: true\nx: "')


class ProvisionTest(unittest.TestCase):
    def test_writes_a_container_readable_file_in_a_private_directory(self) -> None:
        with TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "searxng"
            path = _runtime(FakeRunner([]), config_dir).provision()

            self.assertTrue(path.is_file())
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o644)
                self.assertEqual(config_dir.stat().st_mode & 0o777, 0o700)

    def test_reprovisioning_keeps_the_existing_secret(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = _runtime(FakeRunner([]), Path(tmp) / "searxng")
            first = yaml.safe_load(runtime.provision().read_text())

            second = yaml.safe_load(runtime.provision().read_text())

            self.assertEqual(
                first["server"]["secret_key"], second["server"]["secret_key"]
            )

    def test_start_refuses_before_provisioning(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = _runtime(FakeRunner([]), Path(tmp) / "searxng")

            with self.assertRaisesRegex(SearxngRuntimeError, "/searxng setup"):
                asyncio.run(runtime.start(base_url="http://127.0.0.1:8080"))


class DockerSearxngRuntimeTest(unittest.TestCase):
    def test_preflight_reports_stopped_docker_daemon(self) -> None:
        runner = FakeRunner(
            [
                CommandResult(
                    exit_code=1,
                    stderr=(
                        "Cannot connect to the Docker daemon at "
                        "unix:///var/run/docker.sock. Is the docker daemon running?"
                    ),
                )
            ]
        )

        with TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(
                SearxngRuntimeError, "daemon is not running or is unreachable"
            ):
                asyncio.run(_runtime(runner, Path(tmp)).preflight())

        self.assertEqual(runner.calls[0][0][0:2], ["docker", "info"])

    def test_preflight_reports_missing_docker_cli(self) -> None:
        runner = FakeRunner(
            [
                CommandResult(
                    exit_code=None,
                    error="Could not start 'docker': No such file or directory",
                )
            ]
        )

        with TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(SearxngRuntimeError, "Docker CLI"):
                asyncio.run(_runtime(runner, Path(tmp)).preflight())

    def test_start_publishes_on_loopback_and_mounts_the_settings_file(self) -> None:
        runner = FakeRunner(
            [
                _daemon_ready(),
                _missing_container(),
                CommandResult(exit_code=0),  # docker image inspect
                CommandResult(exit_code=0, stdout="container-id\n"),  # docker run
            ]
        )

        with TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "searxng"
            runtime = _runtime(runner, config_dir)
            runtime.provision()

            info = asyncio.run(runtime.start(base_url="http://localhost:8085"))

        self.assertEqual(info.base_url, "http://127.0.0.1:8085")
        self.assertFalse(info.image_pulled)
        run_args = _call_args(runner, "run")
        self.assertIn("127.0.0.1:8085:8080", run_args)
        self.assertIn("org.kerkov.managed=searxng", run_args)
        self.assertIn("FORCE_OWNERSHIP=false", run_args)
        mount = next(a for a in run_args if a.endswith(f"{CONTAINER_SETTINGS_PATH}:ro"))
        self.assertTrue(mount.startswith(str(config_dir.resolve())))

    def test_start_pulls_a_missing_image(self) -> None:
        runner = FakeRunner(
            [
                _daemon_ready(),
                _missing_container(),
                CommandResult(exit_code=1, stderr="No such image"),
                CommandResult(exit_code=0),  # docker pull
                CommandResult(exit_code=0, stdout="container-id\n"),
            ]
        )

        with TemporaryDirectory() as tmp:
            runtime = _runtime(runner, Path(tmp) / "searxng")
            runtime.provision()

            info = asyncio.run(runtime.start(base_url="http://127.0.0.1:8080"))

        self.assertTrue(info.image_pulled)
        self.assertEqual(
            _call_args(runner, "pull"),
            ["docker", "pull", "docker.io/searxng/searxng:latest"],
        )

    def test_start_replaces_only_a_managed_container(self) -> None:
        runner = FakeRunner(
            [_daemon_ready(), CommandResult(exit_code=0, stdout="|true")]
        )

        with TemporaryDirectory() as tmp:
            runtime = _runtime(runner, Path(tmp) / "searxng")
            runtime.provision()

            with self.assertRaisesRegex(SearxngRuntimeError, "unmanaged container"):
                asyncio.run(runtime.start(base_url="http://127.0.0.1:8080"))

    def test_start_removes_an_existing_managed_container(self) -> None:
        runner = FakeRunner(
            [
                _daemon_ready(),
                _managed_container(running=True),
                CommandResult(exit_code=0),  # docker rm --force
                CommandResult(exit_code=0),  # docker image inspect
                CommandResult(exit_code=0, stdout="container-id\n"),
            ]
        )

        with TemporaryDirectory() as tmp:
            runtime = _runtime(runner, Path(tmp) / "searxng")
            runtime.provision()

            asyncio.run(runtime.start(base_url="http://127.0.0.1:8080"))

        self.assertEqual(
            _call_args(runner, "rm"),
            ["docker", "rm", "--force", "kerkov-searxng"],
        )

    def test_stop_reports_a_missing_container(self) -> None:
        runner = FakeRunner([_daemon_ready(), _missing_container()])

        with TemporaryDirectory() as tmp:
            stopped = asyncio.run(_runtime(runner, Path(tmp)).stop())

        self.assertFalse(stopped)

    def test_stop_removes_the_managed_container(self) -> None:
        runner = FakeRunner(
            [_daemon_ready(), _managed_container(), CommandResult(exit_code=0)]
        )

        with TemporaryDirectory() as tmp:
            stopped = asyncio.run(_runtime(runner, Path(tmp)).stop())

        self.assertTrue(stopped)

    def test_state_reports_a_stopped_container(self) -> None:
        runner = FakeRunner([_daemon_ready(), _managed_container(running=False)])

        with TemporaryDirectory() as tmp:
            state = asyncio.run(_runtime(runner, Path(tmp)).state())

        self.assertTrue(state.exists)
        self.assertFalse(state.running)


class BaseUrlTest(unittest.TestCase):
    def test_normalizes_localhost_and_strips_the_path(self) -> None:
        self.assertEqual(
            normalize_searxng_base_url(" http://localhost:8080/ "),
            "http://127.0.0.1:8080",
        )

    def test_rejects_remote_and_port_less_urls(self) -> None:
        for value in (
            "https://127.0.0.1:8080",
            "http://example.com:8080",
            "http://127.0.0.1",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    normalize_searxng_base_url(value)

    def test_falls_forward_to_a_free_port(self) -> None:
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen(1)
            port = taken.getsockname()[1]

            chosen = choose_loopback_base_url(f"http://127.0.0.1:{port}")

        self.assertNotEqual(chosen, f"http://127.0.0.1:{port}")
        self.assertTrue(chosen.startswith("http://127.0.0.1:"))


async def _never_sleep(_: float) -> None:
    return None


class WaitForSearxngTest(unittest.TestCase):
    def test_returns_the_version_once_the_probe_succeeds(self) -> None:
        clock = iter([0.0, 1.0, 2.0, 3.0, 4.0])
        attempts: list[str] = []

        async def probe(base_url: str, *, timeout_seconds: float) -> str:
            attempts.append(base_url)
            if len(attempts) < 3:
                raise SearxngRuntimeError("connection refused")
            return "2026.3.25"

        with patch("kerkov.core.websearch.searxng_runtime.probe_searxng", new=probe):
            version = asyncio.run(
                wait_for_searxng(
                    "http://127.0.0.1:8080",
                    timeout_seconds=30,
                    now_fn=lambda: next(clock),
                    sleep_fn=_never_sleep,
                )
            )

        self.assertEqual(version, "2026.3.25")
        self.assertEqual(len(attempts), 3)

    def test_reports_the_last_error_on_timeout(self) -> None:
        clock = iter([0.0, 1.0, 99.0])

        async def probe(base_url: str, *, timeout_seconds: float) -> str:
            raise SearxngRuntimeError("connection refused")

        with patch("kerkov.core.websearch.searxng_runtime.probe_searxng", new=probe):
            with self.assertRaisesRegex(SearxngRuntimeError, "connection refused"):
                asyncio.run(
                    wait_for_searxng(
                        "http://127.0.0.1:8080",
                        timeout_seconds=10,
                        now_fn=lambda: next(clock),
                        sleep_fn=_never_sleep,
                    )
                )


if __name__ == "__main__":
    unittest.main()
