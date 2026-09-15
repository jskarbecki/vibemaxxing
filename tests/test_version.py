from __future__ import annotations

import importlib.metadata
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_version_is_single_sourced() -> None:
    declared = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert importlib.metadata.version("vibemaxxing") == declared

    vibe = Path(sys.executable).parent / "vibe"
    proc = subprocess.run([str(vibe), "--version"], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == declared
