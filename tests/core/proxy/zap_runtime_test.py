"""Tests for the managed OWASP ZAP Docker runtime."""

import asyncio
import socket
import unittest
from typing import Sequence

from kerkov.core.proxy.zap_runtime import (
    CommandResult,
    DockerZapRuntime,
    ZapRuntimeError,
    choose_loopback_service_url,
)


class FakeRunner:
    def __init__(self, responses: Sequence[CommandResult]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[list[str], float]] = []

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_seconds: float,
    ) -> CommandResult:
        self.calls.append((list(args), timeout_seconds))
        if not self.responses:
            raise AssertionError(f"Unexpected command: {args}")
        return self.responses.pop(0)


def _runtime(runner: FakeRunner) -> DockerZapRuntime:
    return DockerZapRuntime(
        image="ghcr.io/zaproxy/zaproxy:stable",
        container_name="kerkov-zap",
        runner=runner,
    )


def _missing_container() -> CommandResult:
    return CommandResult(exit_code=1, stderr="Error: No such object: kerkov-zap")


def _managed_container() -> CommandResult:
    return CommandResult(exit_code=0, stdout="zap\n")


class DockerZapRuntimeTest(unittest.TestCase):
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

        with self.assertRaisesRegex(
            ZapRuntimeError,
            "daemon is not running or is unreachable",
        ):
            asyncio.run(_runtime(runner).preflight())

        self.assertEqual(len(runner.calls), 1)
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

        with self.assertRaisesRegex(ZapRuntimeError, "Docker CLI"):
            asyncio.run(_runtime(runner).preflight())

    def test_preflight_treats_timeout_as_unreachable_daemon(self) -> None:
        runner = FakeRunner(
            [
                CommandResult(
                    exit_code=None,
                    timed_out=True,
                    error="Command timed out after 20 seconds",
                )
            ]
        )

        with self.assertRaisesRegex(
            ZapRuntimeError,
            "daemon is not running or is unreachable",
        ):
            asyncio.run(_runtime(runner).preflight())

    def test_starts_with_loopback_publish_and_generated_key(self) -> None:
        runner = FakeRunner(
            [
                CommandResult(exit_code=0, stdout="28.5.2"),
                _missing_container(),
                CommandResult(exit_code=0, stdout="image"),
                CommandResult(exit_code=0, stdout="container-id"),
            ]
        )

        info = asyncio.run(
            _runtime(runner).start(
                service_url="http://127.0.0.1:8090",
                api_key="generated-key",
            )
        )

        self.assertFalse(info.image_pulled)
        run_args = runner.calls[-1][0]
        self.assertIn("127.0.0.1:8090:8090", run_args)
        self.assertIn("ZAP_PORT=8090", run_args)
        self.assertEqual(
            run_args[run_args.index("--add-host") + 1],
            "host.docker.internal:host-gateway",
        )
        self.assertEqual(run_args[run_args.index("-port") + 1], "8090")
        self.assertIn("org.kerkov.managed=zap", run_args)
        self.assertIn("api.key=generated-key", run_args)
        self.assertNotIn("--privileged", run_args)

    def test_pulls_missing_image_before_start(self) -> None:
        runner = FakeRunner(
            [
                CommandResult(exit_code=0),
                _missing_container(),
                CommandResult(exit_code=1, stderr="missing"),
                CommandResult(exit_code=0, stdout="pulled"),
                CommandResult(exit_code=0, stdout="started"),
            ]
        )

        info = asyncio.run(
            _runtime(runner).start(
                service_url="http://127.0.0.1:8090",
                api_key="generated-key",
            )
        )

        self.assertTrue(info.image_pulled)
        self.assertEqual(runner.calls[3][0][:2], ["docker", "pull"])

    def test_replaces_only_existing_managed_container(self) -> None:
        runner = FakeRunner(
            [
                CommandResult(exit_code=0),
                _managed_container(),
                CommandResult(exit_code=0),
                CommandResult(exit_code=0),
                CommandResult(exit_code=0),
            ]
        )

        asyncio.run(
            _runtime(runner).start(
                service_url="http://127.0.0.1:8090",
                api_key="key",
            )
        )

        self.assertEqual(
            runner.calls[2][0],
            ["docker", "rm", "--force", "kerkov-zap"],
        )
        self.assertEqual(
            runner.calls[1][0],
            [
                "docker",
                "inspect",
                "--format",
                '{{ index .Config.Labels "org.kerkov.managed" }}',
                "kerkov-zap",
            ],
        )

    def test_refuses_unmanaged_name_collision(self) -> None:
        unmanaged = CommandResult(
            exit_code=0,
            stdout="<no value>\n",
        )
        runner = FakeRunner([CommandResult(exit_code=0), unmanaged])

        with self.assertRaisesRegex(ZapRuntimeError, "unmanaged"):
            asyncio.run(
                _runtime(runner).start(
                    service_url="http://127.0.0.1:8090",
                    api_key="key",
                )
            )

        self.assertEqual(len(runner.calls), 2)

    def test_redacts_key_from_start_failure(self) -> None:
        runner = FakeRunner(
            [
                CommandResult(exit_code=0),
                _missing_container(),
                CommandResult(exit_code=0),
                CommandResult(exit_code=1, stderr="bad api.key=secret-key"),
            ]
        )

        with self.assertRaises(ZapRuntimeError) as raised:
            asyncio.run(
                _runtime(runner).start(
                    service_url="http://127.0.0.1:8090",
                    api_key="secret-key",
                )
            )

        self.assertNotIn("secret-key", str(raised.exception))
        self.assertIn("[REDACTED]", str(raised.exception))

    def test_stop_reports_absent_container(self) -> None:
        runner = FakeRunner([CommandResult(exit_code=0), _missing_container()])

        self.assertFalse(asyncio.run(_runtime(runner).stop()))

    def test_stop_removes_managed_container(self) -> None:
        runner = FakeRunner(
            [
                CommandResult(exit_code=0),
                _managed_container(),
                CommandResult(exit_code=0),
            ]
        )

        self.assertTrue(asyncio.run(_runtime(runner).stop()))
        self.assertEqual(runner.calls[-1][0][0:3], ["docker", "rm", "--force"])

    def test_requires_http_loopback_runtime_url(self) -> None:
        runner = FakeRunner([])
        for value in [
            "https://127.0.0.1:8090",
            "http://zap.example.test:8090",
        ]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                asyncio.run(_runtime(runner).start(service_url=value, api_key="key"))


class ChooseLoopbackServiceUrlTest(unittest.TestCase):
    def test_uses_preferred_free_port(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.assertEqual(
            choose_loopback_service_url(f"http://127.0.0.1:{port}"),
            f"http://127.0.0.1:{port}",
        )

    def test_skips_occupied_port(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            occupied = listener.getsockname()[1]
            selected = choose_loopback_service_url(
                f"http://127.0.0.1:{occupied}",
                attempts=2,
            )
        self.assertEqual(selected, f"http://127.0.0.1:{occupied + 1}")


if __name__ == "__main__":
    unittest.main()
