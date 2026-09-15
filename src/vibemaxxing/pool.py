"""Pooled headroom across accounts, and the burn-rate forecast over it."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from vibemaxxing.models import AccountState, Sample
from vibemaxxing.usage import Row

WEEKLY_ALL_KIND: Final = "weekly_all"


@dataclass(frozen=True)
class AccountUsage:
    state: AccountState
    rows: tuple[Row, ...]


def pool_remaining(accounts: Iterable[AccountUsage]) -> float:
    """Account-weeks left, summed over the weekly_all row only.

    The session and weekly_scoped rows overlap the weekly_all one, so counting
    them would inflate the pool by however many limit kinds the server ships.
    """
    total = 0.0
    for account in accounts:
        if account.state is not AccountState.OK:
            continue
        for row in account.rows:
            if row.kind == WEEKLY_ALL_KIND and row.percent is not None:
                total += (100 - min(max(row.percent, 0), 100)) / 100
                break
    return total


def dry_in(samples: Sequence[Sample]) -> timedelta | None:
    """Time until the pool reaches zero at the rate between oldest and newest.

    Deliberately not clamped at the next weekly reset: a forecast that stops at
    the boundary reads as "you are fine until then" when the truth is that the
    pool runs out first.
    """
    if len(samples) < 2:
        return None
    oldest = min(samples, key=lambda sample: sample.at_s)
    newest = max(samples, key=lambda sample: sample.at_s)
    span_s = newest.at_s - oldest.at_s
    if span_s <= 0:
        return None
    rate = (oldest.pool - newest.pool) / span_s
    if rate <= 0:
        return None
    return timedelta(seconds=newest.pool / rate)
