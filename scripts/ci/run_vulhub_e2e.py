#!/usr/bin/env python3
"""
Run a gated end-to-end Vulhub exercise against a self-hosted runner.

This harness intentionally uses one allowlisted case at a time. It:
1. Ensures a local Vulhub checkout exists
2. Cleans up any previous lab state for the chosen case
3. Runs the CLI with a fixed prompt instructing the bot to use the Vulhub skills
4. Verifies the lab is reachable and that a harmless proof artifact exists
5. Tears the lab down unless told otherwise
"""

from __future__ import annotations

import argparse
import codecs
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES_PATH = ROOT / "config" / "ci" / "vulhub_e2e_cases.json"
DEFAULT_LOG_DIR = ROOT / "artifacts" / "vulhub-e2e"
DEFAULT_VULHUB_DIR = ROOT / "vulhub"
DEFAULT_TIMEOUT_SECONDS = 420
DEFAULT_HTTP_WAIT_SECONDS = 180


@dataclass
class VulhubCase:
    case_id: str
    cve: str
    lab_path: str
    service: str
    target_url: str
    compose_port: int
    proof_file: str
    healthcheck_url: str
    exploit_goal: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a Vulhub E2E CI case")
    parser.add_argument("--case", dest="case_id", default=None, help="Allowlisted case id")
    parser.add_argument("--cases-file", default=str(DEFAULT_CASES_PATH), help="Path to case config JSON")
    parser.add_argument("--provider", default="codex", help="Provider slash command target")
    parser.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR), help="Directory for captured logs")
    parser.add_argument("--vulhub-dir", default=str(DEFAULT_VULHUB_DIR), help="Path to local Vulhub checkout")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="CLI interaction timeout in seconds")
    parser.add_argument("--http-wait", type=int, default=DEFAULT_HTTP_WAIT_SECONDS, help="Time to wait for HTTP reachability")
    parser.add_argument("--keep-lab", action="store_true", help="Leave the lab running after verification")
    return parser.parse_args()


def load_case(cases_path: Path, requested_case_id: str | None) -> VulhubCase:
    raw = json.loads(cases_path.read_text(encoding="utf-8"))
    case_id = requested_case_id or raw["default_case"]
    case_data: dict[str, Any] = raw["cases"][case_id]
    return VulhubCase(case_id=case_id, **case_data)


def run_command(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=check,
        text=True,
        capture_output=capture_output,
    )


def ensure_vulhub_checkout(vulhub_dir: Path) -> None:
    if (vulhub_dir / ".git").is_dir():
        run_command(["git", "pull", "--ff-only"], cwd=vulhub_dir)
        return

    if vulhub_dir.exists() and any(vulhub_dir.iterdir()):
        raise RuntimeError(
            f"Vulhub directory {vulhub_dir} exists but is not a git checkout; refusing to modify it."
        )

    vulhub_dir.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        ["git", "clone", "--depth", "1", "https://github.com/vulhub/vulhub.git", str(vulhub_dir)]
    )


def build_prompt(case: VulhubCase, vulhub_dir: Path) -> str:
    return textwrap.dedent(
        f"""
        You are running in an authorized self-hosted CI environment on a local machine.

        Use the `vulhub-cve-lab` skill to boot `{case.cve}` from the local Vulhub checkout at `{vulhub_dir}`.
        Verify the target is reachable at `{case.target_url}`.

        Then use the `vulhub-exploit-validator` skill to perform a safe proof of exploitation.
        {case.exploit_goal}

        Keep the lab running after the proof so CI can verify it externally.
        Return a concise report including status, lab_path, target, evidence, and cleanup.
        """
    ).strip()


def log_progress(message: str) -> None:
    print(f"[vulhub-e2e] {message}", flush=True)


def flatten_prompt(prompt: str) -> str:
    return " ".join(line.strip() for line in prompt.splitlines() if line.strip())


def run_cli_interaction(provider: str, prompt: str, timeout_seconds: int, log_path: Path) -> tuple[int, str]:
    command = [sys.executable, "-m", "src.cli.main"]
    single_line_prompt = flatten_prompt(prompt)
    input_text = f"/provider {provider}\n{single_line_prompt}\n"

    proc = subprocess.Popen(
        command,
        cwd=str(ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=False,
        env=os.environ.copy(),
        bufsize=0,
    )

    assert proc.stdin is not None
    assert proc.stdout is not None

    log_progress(f"Starting CLI with provider={provider}")
    proc.stdin.write(input_text.encode("utf-8"))

    output_chunks: list[str] = []
    deadline = time.time() + timeout_seconds
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    prompt_pattern = re.compile(r"\bYou:\s*$", re.MULTILINE)
    prompt_count = 0
    quit_sent = False
    recent_output = ""

    try:
        while True:
            if time.time() > deadline:
                raise subprocess.TimeoutExpired(command, timeout_seconds)

            events = selector.select(timeout=0.2)
            if events:
                for key, _ in events:
                    chunk = os.read(key.fileobj.fileno(), 4096)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    text = decoder.decode(chunk)
                    if text:
                        print(text, end="", flush=True)
                        output_chunks.append(text)
                        combined_output = recent_output + text
                        prompt_count += max(
                            0,
                            len(prompt_pattern.findall(combined_output))
                            - len(prompt_pattern.findall(recent_output)),
                        )
                        recent_output = combined_output[-256:]

            if not quit_sent and prompt_count >= 3:
                proc.stdin.write(b"/q\n")
                proc.stdin.close()
                quit_sent = True

            if proc.poll() is not None and not selector.get_map():
                break
        exit_code = proc.wait()
    except subprocess.TimeoutExpired:
        log_progress("CLI timed out, sending SIGINT")
        proc.send_signal(signal.SIGINT)
        try:
            remaining_output, _ = proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            log_progress("CLI did not exit after SIGINT, killing process")
            proc.kill()
            remaining_output, _ = proc.communicate()
        if remaining_output:
            text = decoder.decode(remaining_output)
            if text:
                print(text, end="", flush=True)
                output_chunks.append(text)
        exit_code = proc.returncode or 0
    finally:
        selector.close()

    trailing_text = decoder.decode(b"", final=True)
    if trailing_text:
        print(trailing_text, end="", flush=True)
        output_chunks.append(trailing_text)

    output = "".join(output_chunks)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(output, encoding="utf-8")
    return exit_code, output


def tail_text(text: str, *, lines: int = 80) -> str:
    split_lines = text.splitlines()
    if len(split_lines) <= lines:
        return text
    return "\n".join(split_lines[-lines:])


def wait_for_http(url: str, timeout_seconds: int) -> None:
    deadline = time.time() + timeout_seconds
    last_error = ""
    while time.time() < deadline:
        try:
            with urlopen(url, timeout=5) as response:
                status = getattr(response, "status", 200)
                if status < 500:
                    return
        except URLError as exc:
            last_error = str(exc)
        except Exception as exc:  # pragma: no cover - defensive logging path
            last_error = str(exc)
        time.sleep(3)
    raise RuntimeError(f"Timed out waiting for HTTP reachability at {url}: {last_error}")


def docker_compose_cmd(lab_dir: Path, *args: str) -> list[str]:
    return ["docker", "compose", *args]


def compose_down(lab_dir: Path) -> None:
    try:
        run_command(docker_compose_cmd(lab_dir, "down", "-v"), cwd=lab_dir, check=False)
    except Exception:
        pass


def ensure_lab_running(lab_dir: Path) -> None:
    result = run_command(docker_compose_cmd(lab_dir, "ps"), cwd=lab_dir)
    if "Up" not in result.stdout and "running" not in result.stdout.lower():
        raise RuntimeError(f"docker compose ps did not show a running service:\n{result.stdout}")


def ensure_proof_file(lab_dir: Path, service: str, proof_file: str) -> None:
    result = run_command(
        docker_compose_cmd(lab_dir, "exec", "-T", service, "test", "-f", proof_file),
        cwd=lab_dir,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Expected proof file {proof_file} was not found in service {service}.")


def missing_output_markers(output: str, case: VulhubCase) -> list[str]:
    required_markers = [
        case.cve,
        "status",
        "target",
        "cleanup",
    ]
    return [marker for marker in required_markers if marker not in output]


def main() -> int:
    args = parse_args()
    cases_path = Path(args.cases_file).resolve()
    vulhub_dir = Path(args.vulhub_dir).resolve()
    log_dir = Path(args.log_dir).resolve()

    case = load_case(cases_path, args.case_id)
    lab_dir = vulhub_dir / case.lab_path
    log_path = log_dir / f"{case.case_id}.log"

    ensure_vulhub_checkout(vulhub_dir)
    if not lab_dir.is_dir():
        raise RuntimeError(f"Expected Vulhub lab directory does not exist: {lab_dir}")

    log_progress(f"Using case={case.case_id} cve={case.cve} lab_dir={lab_dir}")
    log_progress("Cleaning up any previous lab state")
    compose_down(lab_dir)

    try:
        prompt = build_prompt(case, vulhub_dir)
        exit_code, output = run_cli_interaction(args.provider, prompt, args.timeout, log_path)
        if exit_code not in (0, 130):
            log_tail = tail_text(output)
            raise RuntimeError(
                f"CLI exited with unexpected status {exit_code}. See {log_path}\n"
                f"--- CLI log tail ---\n{log_tail}"
            )

        missing_markers = missing_output_markers(output, case)
        if missing_markers:
            print(
                "Warning: CLI output missed expected markers: "
                f"{', '.join(missing_markers)}. Continuing with external verification."
            )

        log_progress(f"Waiting for HTTP reachability at {case.healthcheck_url}")
        wait_for_http(case.healthcheck_url, args.http_wait)
        log_progress("Checking docker compose status")
        ensure_lab_running(lab_dir)
        log_progress(f"Checking proof artifact {case.proof_file} in service {case.service}")
        ensure_proof_file(lab_dir, case.service, case.proof_file)
    finally:
        if not args.keep_lab:
            log_progress("Tearing down lab")
            compose_down(lab_dir)

    print(f"Vulhub E2E case succeeded: {case.case_id}")
    print(f"Log written to: {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
