from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.vulhub_inventory import build_vulhub_inventory


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_build_vulhub_inventory_discovers_and_summarizes_labs(tmp_path: Path) -> None:
    vulhub_dir = tmp_path / "vulhub"

    _write(
        vulhub_dir / "phpunit" / "CVE-2017-9841" / "docker-compose.yml",
        "services:\n  web:\n    image: demo\n",
    )
    _write(
        vulhub_dir / "phpunit" / "CVE-2017-9841" / "README.md",
        "# PHPUnit Remote Code Execution (CVE-2017-9841)\n",
    )
    _write(vulhub_dir / "phpunit" / "CVE-2017-9841" / "poc.py", "print('demo')\n")

    _write(
        vulhub_dir / "flask" / "ssti" / "docker-compose.yml",
        "services:\n  web:\n    image: demo\n",
    )
    _write(
        vulhub_dir / "flask" / "ssti" / "README.md",
        "# Flask (Jinja2) Server-Side Template Injection\n",
    )
    _write(vulhub_dir / "flask" / "ssti" / "exploit.py", "print('demo')\n")

    inventory = build_vulhub_inventory(vulhub_dir)

    assert inventory["summary"] == {"lab_count": 2, "product_count": 2}
    assert inventory["products"] == [
        {"product": "flask", "lab_count": 1},
        {"product": "phpunit", "lab_count": 1},
    ]

    flask_lab = inventory["labs"][0]
    assert flask_lab["inventory_id"] == "flask/ssti"
    assert flask_lab["primary_identifier"] == "ssti"
    assert flask_lab["poc_files"] == ["exploit.py"]

    phpunit_lab = inventory["labs"][1]
    assert phpunit_lab["inventory_id"] == "phpunit/CVE-2017-9841"
    assert phpunit_lab["primary_identifier"] == "CVE-2017-9841"
    assert phpunit_lab["readme_path"] == "phpunit/CVE-2017-9841/README.md"
    assert phpunit_lab["poc_files"] == ["poc.py"]
