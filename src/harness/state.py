"""state.json read/write helpers.

Driver and HarnessStateTool both touch this file. Validation goes through
the Pydantic model; callers get HarnessState back, never raw dict.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.harness.models import HarnessState


def state_path(run_dir: Path) -> Path:
    return run_dir / "state.json"


def load_state(run_dir: Path) -> HarnessState:
    """Return the run's current state, or a fresh state if state.json is absent."""
    path = state_path(run_dir)
    if not path.is_file():
        return HarnessState()
    raw = json.loads(path.read_text(encoding="utf-8"))
    return HarnessState(**raw)


def save_state(run_dir: Path, state: HarnessState) -> None:
    path = state_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(state.model_dump_json(indent=2), encoding="utf-8")
