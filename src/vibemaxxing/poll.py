"""The poll planner.

Pure: it never sleeps and never reads the clock. Every decision is a function of
the ``now_s`` the caller hands in, which is the only way a simulated clock can
drive the cadence in a test.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

POLL_FLOOR_S: Final = 60.0
MIN_GAP_S: Final = 10.0
BACKOFF_S: Final = (60.0, 120.0, 240.0, 480.0)


@dataclass(frozen=True)
class Due:
    alias: str
    at_s: float
    backoff: bool


class Scheduler:
    def __init__(self, aliases: Sequence[str], *, start_s: float) -> None:
        count = len(aliases) or 1
        # Floor and gap are both hard, so past six accounts the stagger widens to
        # MIN_GAP_S and the cycle lengthens rather than the gap shrinking.
        step_s = max(POLL_FLOOR_S / count, MIN_GAP_S)
        self._cycle_s = count * step_s
        self._due: dict[str, float] = {
            alias: start_s + i * step_s for i, alias in enumerate(aliases)
        }
        self._issued: dict[str, float] = {}
        self._strikes: dict[str, int] = dict.fromkeys(aliases, 0)
        self._streak_start: dict[str, float] = {}
        self._last_issue_s = -math.inf

    def next_due(self, *, now_s: float) -> Due | None:
        if now_s - self._last_issue_s < MIN_GAP_S:
            return None
        ready = [(at_s, alias) for alias, at_s in self._due.items() if at_s <= now_s]
        if not ready:
            return None
        _, alias = min(ready)
        # Nothing is due again until the caller reports the outcome.
        self._due[alias] = math.inf
        self._issued[alias] = now_s
        self._last_issue_s = now_s
        return Due(alias, now_s, self._strikes[alias] > 0)

    def record_success(self, alias: str, *, now_s: float) -> None:
        self._clear_streak(alias)
        # Measured from the request, not from its outcome, so a slow response
        # does not drag the whole stagger later.
        self._due[alias] = self._issued[alias] + self._cycle_s

    def record_error(self, alias: str, *, now_s: float) -> None:
        strikes = self._strikes[alias] + 1
        self._strikes[alias] = strikes
        # Offsets run from the first error of the streak, so the retries land
        # 60/120/240/480 s after the account started failing; past the table the
        # spacing holds at the last step.
        streak_start_s = self._streak_start.setdefault(alias, now_s)
        if strikes <= len(BACKOFF_S):
            self._due[alias] = streak_start_s + BACKOFF_S[strikes - 1]
        else:
            self._due[alias] = now_s + BACKOFF_S[-1]

    def record_exhausted(self, alias: str, *, now_s: float, resets_at_s: float) -> None:
        self._clear_streak(alias)
        # A reset already in the past must still not breach the floor.
        self._due[alias] = max(resets_at_s, self._issued[alias] + POLL_FLOOR_S)

    def _clear_streak(self, alias: str) -> None:
        self._strikes[alias] = 0
        self._streak_start.pop(alias, None)
