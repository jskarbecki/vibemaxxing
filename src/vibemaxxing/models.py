"""Small value types shared by more than one slice. Nothing here imports the package."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AccountState(StrEnum):
    OK = "ok"
    NEEDS_LOGIN = "needs_login"
    ERROR = "error"


@dataclass(frozen=True)
class Sample:
    """One pooled-headroom measurement. Produced by history, consumed by pool."""

    at_s: float
    pool: float
