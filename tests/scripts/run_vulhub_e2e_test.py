from __future__ import annotations

import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ci.run_vulhub_e2e import load_case


def test_load_case_merges_inventory_and_case_overlay(tmp_path: Path) -> None:
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(
        json.dumps(
            {
                "labs": [
                    {
                        "inventory_id": "flask/ssti",
                        "cve": "ssti",
                        "lab_path": "flask/ssti",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    cases_path = tmp_path / "cases.json"
    cases_path.write_text(
        json.dumps(
            {
                "default_case": "flask-ssti",
                "cases": {
                    "flask-ssti": {
                        "inventory_id": "flask/ssti",
                        "service": "web",
                        "target_url": "http://127.0.0.1:8000/",
                        "proof_file": "/tmp/kerkov-ci-proof",
                        "healthcheck_url": "http://127.0.0.1:8000/",
                        "exploit_goal": "Create a proof file.",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    case = load_case(cases_path, inventory_path, None)

    assert case.case_id == "flask-ssti"
    assert case.inventory_id == "flask/ssti"
    assert case.cve == "ssti"
    assert case.lab_path == "flask/ssti"
    assert case.service == "web"
