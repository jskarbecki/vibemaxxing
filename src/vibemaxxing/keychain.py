"""The credential store Claude Code itself reads: the macOS Keychain, or a file.

``CLAUDE_CONFIG_DIR`` is never read, never set and never honoured. With it set
Claude Code scopes the item to a hashed service name and ``NO_KEYCHAIN=1`` does
not reliably produce a plaintext file — measured. We swap credentials, we do not
isolate config directories, so only the fixed-name item matters.
"""

from __future__ import annotations

import getpass
import subprocess
import sys
from pathlib import Path
from typing import Final, Protocol

from vibemaxxing.errors import VibeError
from vibemaxxing.fsutil import private_dir, write_private

CLAUDE_CODE_KEYCHAIN_SERVICE: Final = "Claude Code-credentials"
SECURITY_BIN: Final = "/usr/bin/security"
SECURITY_TIMEOUT_S: Final = 10.0
SECURITY_ABSENT_CODE: Final = 44

_RECOVERY: Final = "vibe add"


class KeychainPort(Protocol):
    def read(self) -> str | None: ...

    def write(self, blob: str) -> None: ...


class MacKeychain:
    def read(self) -> str | None:
        result = self._run(
            [
                SECURITY_BIN,
                "find-generic-password",
                "-a",
                getpass.getuser(),
                "-w",
                "-s",
                CLAUDE_CODE_KEYCHAIN_SERVICE,
            ]
        )
        if result.returncode == SECURITY_ABSENT_CODE:
            return None
        if result.returncode != 0:
            raise VibeError(
                f"the macOS Keychain refused to read {CLAUDE_CODE_KEYCHAIN_SERVICE!r} "
                f"(security exit {result.returncode})",
                _RECOVERY,
            )
        return result.stdout[:-1] if result.stdout.endswith("\n") else result.stdout

    def write(self, blob: str) -> None:
        # The blob sits in argv, where `ps` exposes it to any local user for the
        # duration of one exec. That is a real cross-uid exposure and it is the
        # least bad option measured: `security -i` silently truncates at ~4005
        # bytes and stores the truncated credential, the promptless `-w` form
        # truncates at 128, and Security.framework cannot touch an item this
        # process did not create without a GUI prompt on every switch. A path
        # that can only corrupt is worse than an exposure that is documented.
        result = self._run(
            [
                SECURITY_BIN,
                "add-generic-password",
                "-U",
                "-a",
                getpass.getuser(),
                "-s",
                CLAUDE_CODE_KEYCHAIN_SERVICE,
                "-w",
                blob,
            ]
        )
        if result.returncode != 0:
            raise VibeError(
                f"the macOS Keychain refused to write {CLAUDE_CODE_KEYCHAIN_SERVICE!r} "
                f"(security exit {result.returncode})",
                _RECOVERY,
            )

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                argv,
                capture_output=True,
                text=True,
                # Explicit, not the process locale: write encodes the blob as
                # UTF-8 into argv, so decoding the read with whatever LANG says
                # is an asymmetric round trip that mangles any non-ASCII byte.
                encoding="utf-8",
                timeout=SECURITY_TIMEOUT_S,
                check=False,
            )
        # UnicodeDecodeError: a non-UTF-8 item must read as unreadable, not escape as a
        # traceback from every dashboard poll.
        except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError) as exc:
            raise VibeError(
                f"could not run {SECURITY_BIN}: {exc.__class__.__name__}",
                _RECOVERY,
            ) from None


class FileKeychain:
    def _path(self) -> Path:
        return Path.home() / ".claude" / ".credentials.json"

    def read(self) -> str | None:
        try:
            return self._path().read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as exc:
            raise VibeError(
                f"could not read {self._path()}: {exc.__class__.__name__}",
                _RECOVERY,
            ) from None

    def write(self, blob: str) -> None:
        path = self._path()
        private_dir(path.parent)
        try:
            write_private(path, blob)
        except OSError as exc:
            raise VibeError(
                f"could not write {path}: {exc.__class__.__name__}",
                _RECOVERY,
            ) from None


def default_port() -> KeychainPort:
    # Bound to a local str on purpose: mypy resolves a bare `sys.platform`
    # comparison against the checking host, which makes the other two branches
    # statically unreachable and unverifiable.
    platform: str = sys.platform
    if platform == "win32":
        raise VibeError(
            "vibemaxxing has no Windows credential store and never writes one",
            "vibe --help",
        )
    if platform == "darwin":
        return MacKeychain()
    return FileKeychain()
