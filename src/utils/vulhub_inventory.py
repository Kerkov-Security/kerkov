from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any


IDENTIFIER_PATTERN = re.compile(
    r"(?i)(CVE-\d{4}-\d+|CNVD-\d{4}-\d+|WooYun-\d{4}-\d+|xianzhi-\d{4}-\d+-\d+)"
)
POC_FILENAMES = {"poc.py", "exp.py", "exploit.py"}


def _read_readme_title(readme_path: Path) -> str | None:
    if not readme_path.is_file():
        return None

    for line in readme_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def _extract_primary_identifier(slug: str, title: str | None) -> str:
    for candidate in (slug, title or ""):
        match = IDENTIFIER_PATTERN.search(candidate)
        if match:
            return match.group(1)
    return slug


def _find_git_value(vulhub_dir: Path, args: list[str]) -> str | None:
    git_dir = vulhub_dir / ".git"
    if not git_dir.exists():
        return None

    result = subprocess.run(
        ["git", *args],
        cwd=str(vulhub_dir),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None

    value = result.stdout.strip()
    return value or None


def build_vulhub_inventory(vulhub_dir: Path, *, source_label: str | None = None) -> dict[str, Any]:
    labs: list[dict[str, Any]] = []

    for compose_path in sorted(vulhub_dir.glob("*/*/docker-compose.yml")):
        lab_dir = compose_path.parent
        relative_lab_dir = lab_dir.relative_to(vulhub_dir).as_posix()
        product, slug = relative_lab_dir.split("/", maxsplit=1)

        readme_path = lab_dir / "README.md"
        readme_zh_path = lab_dir / "README.zh-cn.md"
        dockerfile_path = lab_dir / "Dockerfile"

        title = _read_readme_title(readme_path)
        poc_files = sorted(
            path.name
            for path in lab_dir.iterdir()
            if path.is_file()
            and (
                path.name in POC_FILENAMES
                or path.suffix in {".py", ".sh"}
                and path.name not in {"docker-entrypoint.sh"}
            )
        )

        labs.append(
            {
                "inventory_id": relative_lab_dir,
                "product": product,
                "slug": slug,
                "primary_identifier": _extract_primary_identifier(slug, title),
                "lab_path": relative_lab_dir,
                "title": title,
                "compose_path": compose_path.relative_to(vulhub_dir).as_posix(),
                "dockerfile_path": (
                    dockerfile_path.relative_to(vulhub_dir).as_posix()
                    if dockerfile_path.is_file()
                    else None
                ),
                "readme_path": (
                    readme_path.relative_to(vulhub_dir).as_posix()
                    if readme_path.is_file()
                    else None
                ),
                "readme_zh_path": (
                    readme_zh_path.relative_to(vulhub_dir).as_posix()
                    if readme_zh_path.is_file()
                    else None
                ),
                "poc_files": poc_files,
            }
        )

    product_counts = Counter(lab["product"] for lab in labs)
    products = [
        {"product": product, "lab_count": count}
        for product, count in sorted(product_counts.items(), key=lambda item: (-item[1], item[0]))
    ]

    return {
        "schema_version": 1,
        "source": {
            "vulhub_dir": source_label or str(vulhub_dir),
            "git_commit": _find_git_value(vulhub_dir, ["rev-parse", "HEAD"]),
            "git_branch": _find_git_value(vulhub_dir, ["branch", "--show-current"]),
        },
        "summary": {
            "lab_count": len(labs),
            "product_count": len(products),
        },
        "products": products,
        "labs": labs,
    }


def write_vulhub_inventory(output_path: Path, inventory: dict[str, Any]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8")
