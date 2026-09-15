"""Autouse guard: no test may touch the live store, the live Claude Code config,
or the real Keychain. AC19."""

from __future__ import annotations

import builtins
import os
import sqlite3
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

from vibemaxxing import redact

# Captured at import, before any test can monkeypatch HOME.
REAL_HOME: Final = Path(os.path.expanduser("~"))
BLOCKED_PATHS: Final = (
    REAL_HOME / ".vibemaxxing",
    REAL_HOME / ".claude",
    REAL_HOME / ".claude.json",
)
BLOCKED_BINARIES: Final = ("security",)


class RealPathAccessError(AssertionError):
    """A test reached for a live path or the real Keychain."""


def _check_path(target: object, node_id: str) -> None:
    if isinstance(target, int):
        return
    try:
        raw = os.fspath(target)  # type: ignore[arg-type]
    except TypeError:
        return
    text = raw.decode() if isinstance(raw, bytes) else raw
    resolved = Path(os.path.abspath(os.path.expanduser(text)))
    for blocked in BLOCKED_PATHS:
        if resolved == blocked or blocked in resolved.parents:
            raise RealPathAccessError(f"{node_id} touched the live path {resolved}")


def _check_argv(args: object, node_id: str) -> None:
    first = args[0] if isinstance(args, (list, tuple)) and args else args
    if not isinstance(first, (str, bytes, os.PathLike)):
        return
    name = os.path.basename(os.fspath(first))
    name = name.decode() if isinstance(name, bytes) else name
    if name in BLOCKED_BINARIES:
        raise RealPathAccessError(f"{node_id} invoked the real Keychain via {name!r}")


@pytest.fixture(autouse=True)
def guard_live_paths(request: pytest.FixtureRequest) -> Iterator[None]:
    node_id = request.node.nodeid
    real_open, real_os_open = builtins.open, os.open
    real_connect, real_run, real_popen = sqlite3.connect, subprocess.run, subprocess.Popen
    # Path.read_text/write_text/read_bytes all funnel through Path.open, which
    # calls io.open directly rather than builtins.open -- so patching builtins
    # alone let a test read the real ~/.claude.json. Verified before this fix.
    real_path_open = Path.open

    def guarded_open(file, *args, **kwargs):
        _check_path(file, node_id)
        return real_open(file, *args, **kwargs)

    def guarded_os_open(path, *args, **kwargs):
        _check_path(path, node_id)
        return real_os_open(path, *args, **kwargs)

    def guarded_connect(database, *args, **kwargs):
        _check_path(database, node_id)
        return real_connect(database, *args, **kwargs)

    def guarded_path_open(self, *args, **kwargs):
        _check_path(self, node_id)
        return real_path_open(self, *args, **kwargs)

    def guarded_run(args, *rest, **kwargs):
        _check_argv(args, node_id)
        return real_run(args, *rest, **kwargs)

    def guarded_popen(args, *rest, **kwargs):
        _check_argv(args, node_id)
        return real_popen(args, *rest, **kwargs)

    builtins.open = guarded_open
    os.open = guarded_os_open
    Path.open = guarded_path_open  # type: ignore[method-assign]
    sqlite3.connect = guarded_connect
    subprocess.run = guarded_run
    subprocess.Popen = guarded_popen
    try:
        yield
    finally:
        Path.open = real_path_open  # type: ignore[method-assign]
        builtins.open, os.open = real_open, real_os_open
        sqlite3.connect, subprocess.run, subprocess.Popen = real_connect, real_run, real_popen


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway HOME. store.store_root() resolves under it, so no test can
    reach the live store even by accident."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("USERPROFILE", raising=False)
    return home


@pytest.fixture(autouse=True)
def clean_secret_registry() -> Iterator[None]:
    # The registry is process-global; a sentinel registered by one test would
    # otherwise redact an unrelated test's output and mask a real leak.
    redact._registry.clear()
    yield
    redact._registry.clear()
