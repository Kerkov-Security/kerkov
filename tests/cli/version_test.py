"""--version exits before importing providers or touching engagement state."""

import os
import subprocess
import sys
from importlib.metadata import version


def test_version_from_unrelated_directory_without_runtime_imports(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys\n"
                "from kerkov.cli.main import parse_args\n"
                "try:\n"
                "    parse_args(['--version'])\n"
                "except SystemExit as exc:\n"
                "    assert exc.code == 0\n"
                "assert 'kerkov.cli.kerkov_cli' not in sys.modules\n"
                "assert 'kerkov.providers' not in sys.modules\n"
            ),
        ],
        cwd=tmp_path,
        env={**os.environ, "HOME": str(tmp_path)},
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"kerkov {version('kerkov')}"
    assert result.stderr == ""
    assert list(tmp_path.iterdir()) == []
