#!/bin/sh
set -eu

required_bins="curl git jq nmap openvpn ping python3 r2 sqlmap uv"
for required_bin in ${required_bins}; do
    if ! command -v "${required_bin}" >/dev/null 2>&1; then
        echo "missing required binary: ${required_bin}" >&2
        exit 1
    fi
done

kerkov --help >/dev/null

python3 - <<'PY'
import asyncio
from pathlib import Path

from kerkov.context.skills.skill_manager import SkillManager
from playwright.async_api import async_playwright


project_root = Path("/opt/kerkov")
for required_path in ("agents", "config", "skills", "src"):
    path = project_root / required_path
    if not path.exists():
        raise SystemExit(f"missing Kerkov runtime path: {path}")

manager = SkillManager()
skills = manager.list_skills()
if not skills:
    raise SystemExit("no built-in Kerkov skills were loaded")

unexpected_missing = {}
# Docker-backed lab skills intentionally remain unavailable: the default
# container does not contain a daemon or mount the host Docker socket.
for skill in skills:
    eligibility = manager.check_eligibility(skill)
    missing_bins = [
        binary for binary in eligibility.missing_bins if binary != "docker"
    ]
    if missing_bins:
        unexpected_missing[skill.name] = missing_bins
if unexpected_missing:
    raise SystemExit(f"built-in skills have missing binaries: {unexpected_missing}")


async def browser_smoke_test() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("data:text/html,<title>kerkov</title>")
        if await page.title() != "kerkov":
            raise SystemExit("Playwright Chromium smoke test returned the wrong title")
        await browser.close()


asyncio.run(browser_smoke_test())
PY
