"""Error taxonomy and exit codes. Every user-facing error carries a recovery command."""

from __future__ import annotations

from typing import ClassVar

from vibemaxxing.redact import scrub


class VibeError(Exception):
    exit_code: ClassVar[int] = 1
    code: ClassVar[str] = "error"

    def __init__(self, message: str, recovery: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.recovery = recovery

    def render(self) -> str:
        if self.recovery is None:
            return scrub(self.message)
        return scrub(f"{self.message}\n  run: {self.recovery}")


class UsageError(VibeError):
    exit_code: ClassVar[int] = 2
    code: ClassVar[str] = "usage"


class PasteFormatError(UsageError):
    code: ClassVar[str] = "paste_format"


class StateMismatchError(UsageError):
    code: ClassVar[str] = "state_mismatch"


class NeedsLoginError(VibeError):
    exit_code: ClassVar[int] = 3
    code: ClassVar[str] = "needs_login"


class NotFoundError(VibeError):
    exit_code: ClassVar[int] = 4
    code: ClassVar[str] = "not_found"


class StoreError(VibeError):
    exit_code: ClassVar[int] = 5
    code: ClassVar[str] = "store"


class NetworkError(VibeError):
    exit_code: ClassVar[int] = 6
    code: ClassVar[str] = "network"
