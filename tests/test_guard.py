from __future__ import annotations

import sqlite3
import subprocess

import pytest

from tests.conftest import REAL_HOME, RealPathAccessError

LIVE_PATHS = (
    REAL_HOME / ".vibemaxxing" / "accounts" / "work.json",
    REAL_HOME / ".claude" / ".credentials.json",
    REAL_HOME / ".claude.json",
)


def test_real_store_guard_blocks_live_paths() -> None:
    for path in LIVE_PATHS:
        with pytest.raises(RealPathAccessError), open(path):
            pass
    with pytest.raises(RealPathAccessError):
        sqlite3.connect(str(REAL_HOME / ".vibemaxxing" / "history.db"))
    with pytest.raises(RealPathAccessError):
        subprocess.run(["/usr/bin/security", "find-generic-password"], check=False)
