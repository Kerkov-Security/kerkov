"""Redirects the paths Kerkov defaults to under `~/.kerkov` into a temp dir.

Imported by both pytest and `python -m unittest`, so it runs before any test
builds a store or a workspace. Without it, tests leave engagement and workspace
directories in the developer's real home, where `--continue` then finds them.
"""

import os
import tempfile
from pathlib import Path

_ROOT = Path(tempfile.mkdtemp(prefix="kerkov-tests-"))

os.environ.setdefault("WORKSPACE_DIR", str(_ROOT / "workspaces"))

import kerkov.cli.engagement_store as _engagement_store  # noqa: E402

PRODUCTION_STORE_DIR = _engagement_store.DEFAULT_STORE_DIR
_engagement_store.DEFAULT_STORE_DIR = _ROOT / "engagements"
