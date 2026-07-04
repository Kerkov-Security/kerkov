import json
from pathlib import Path
from typing import Any, Optional


def resolve_repo_path(path_value: str, start_dir: Optional[Path] = None) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path

    current = start_dir or Path.cwd()
    for _ in range(5):
        candidate = current / path
        if candidate.exists():
            return candidate
        current = current.parent

    return (start_dir or Path.cwd()) / path


def load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    return data if isinstance(data, dict) else {}


def write_json_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
