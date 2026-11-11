"""Browser profiles remain exclusive across processes and release on close."""

import subprocess
import sys
from pathlib import Path

from kerkov.core.browser.persistent_profile import ProfileLease, prepare_profile


def test_profile_lock_excludes_other_processes_and_releases_on_close(
    tmp_path: Path,
) -> None:
    profile = prepare_profile(tmp_path / "profiles", "target")
    script = """
import sys
from pathlib import Path
from kerkov.core.browser.persistent_profile import ProfileLease
try:
    lease = ProfileLease(Path(sys.argv[1]))
except OSError:
    sys.exit(3)
lease.close()
"""
    lease = ProfileLease(profile)
    try:
        blocked = subprocess.run(
            [sys.executable, "-c", script, str(profile)],
            capture_output=True,
            timeout=10,
        )
        assert blocked.returncode == 3, blocked.stderr.decode()
    finally:
        lease.close()
    available = subprocess.run(
        [sys.executable, "-c", script, str(profile)], capture_output=True, timeout=10
    )
    assert available.returncode == 0, available.stderr.decode()
    lease.close()
