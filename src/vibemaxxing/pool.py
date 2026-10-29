"""Pooled headroom across accounts, the burn-rate forecast over it, and the decision
to leave the live account for another. Pure: nothing here reads a clock or a file."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal

from vibemaxxing.models import AccountState, Sample
from vibemaxxing.poll import USAGE_FRESH_S
from vibemaxxing.usage import Row

WEEKLY_ALL_KIND: Final = "weekly_all"
# The rows a switch is about. weekly_scoped is one model's own cap and kinds the server
# adds later are not known to mean "this account is spent", so neither moves anyone.
TRIGGER_KINDS: Final = ("session", WEEKLY_ALL_KIND)

REASON_OFF: Final = "auto-switch is off"
REASON_LIVE_UNKNOWN: Final = "live account unknown"

DEFAULT_THRESHOLD: Final = 99
DEFAULT_COOLDOWN_S: Final = 900
CANDIDATE_MARGIN: Final = 5
MIN_THRESHOLD: Final = 50
MAX_THRESHOLD: Final = 100


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


@dataclass(frozen=True)
class Settings:
    """`~/.vibemaxxing/settings.json`. The defaults are also what an invalid file means."""

    auto_switch: bool = False
    threshold: int = DEFAULT_THRESHOLD
    cooldown_s: int = DEFAULT_COOLDOWN_S
    excluded: tuple[str, ...] = ()


@dataclass(frozen=True)
class LastSwitch:
    """`~/.vibemaxxing/auto.json`: the last switch, whoever made it.

    ``until_s`` is when the rows that limited ``from_alias`` have all reset, or None
    when that was not known. The account just left stays out of the running until then.
    """

    from_alias: str
    to_alias: str
    at_s: float
    reason: str
    until_s: float | None


@dataclass(frozen=True)
class Reading:
    """One account as `decide` sees it. ``rows`` is None when there is no usable answer."""

    alias: str
    state: AccountState
    rows: tuple[Row, ...] | None
    updated_at: float | None
    # False for an account stored without an uuid. After landing on one, ~/.claude.json
    # still names the account that was left, so vibe could not tell who is live; it is
    # never chosen as a target on its own.
    identified: bool = True


@dataclass(frozen=True)
class Decision:
    action: Literal["stay", "switch"]
    target: str | None
    reason: str


def _stay(reason: str) -> Decision:
    return Decision("stay", None, reason)


def parse_stamp(stamp: str) -> float | None:
    """A timestamp the server sent, as epoch seconds; None when it is not one."""
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _reset_s(row: Row) -> float | None:
    # An unreadable reset is an unknown one, which callers treat as "has not happened yet".
    return None if row.resets_at is None else parse_stamp(row.resets_at)


def _over(rows: Iterable[Row], threshold: int) -> list[Row]:
    return [
        row
        for row in rows
        if row.kind in TRIGGER_KINDS and row.percent is not None and row.percent >= threshold
    ]


def blocking(rows: Iterable[Row], threshold: int, now_s: float) -> tuple[Row, ...]:
    """The trigger rows at or over ``threshold`` whose window has not reset yet.

    A reading is a floor until its own row resets: usage only falls when the window
    rolls over. So 99% with a reset still ahead is true however old the reading is,
    and one whose reset has passed says nothing about now.
    """
    found: list[Row] = []
    for row in _over(rows, threshold):
        reset_s = _reset_s(row)
        if reset_s is None or reset_s > now_s:
            found.append(row)
    return tuple(found)


def limited_until(rows: Iterable[Row], threshold: int, now_s: float) -> float | None:
    """When every row holding the account over ``threshold`` has reset; None if unknown."""
    resets = [_reset_s(row) for row in blocking(rows, threshold, now_s)]
    known = [reset_s for reset_s in resets if reset_s is not None]
    if not known or len(known) != len(resets):
        return None
    return max(known)


def trigger_percent(rows: Iterable[Row]) -> int | None:
    """The fullest of the session and weekly_all rows; None when any is unknown or none exist."""
    percents = [row.percent for row in rows if row.kind in TRIGGER_KINDS]
    known = [percent for percent in percents if percent is not None]
    if not known or len(known) != len(percents):
        return None
    return max(known)


def _fullness(
    reading: Reading, *, settings: Settings, live: str, now_s: float, last: LastSwitch | None
) -> int | None:
    """How full a candidate is, or None when it is not one.

    State ok is what makes the credential usable: envelope.collect has already refused
    a lapsed login and refreshed an expired token before it hands over a reading.
    """
    if reading.alias == live or reading.alias in settings.excluded or not reading.identified:
        return None
    if reading.state is not AccountState.OK or reading.rows is None or reading.updated_at is None:
        return None
    if not 0 <= now_s - reading.updated_at < USAGE_FRESH_S:
        return None
    # An account this close to the threshold would cross it within minutes, and the
    # cooldown would then keep the next switch from leaving it.
    if _over(reading.rows, settings.threshold - CANDIDATE_MARGIN):
        return None
    if (
        last is not None
        and last.from_alias == reading.alias
        and last.until_s is not None
        and now_s < last.until_s
    ):
        return None
    return trigger_percent(reading.rows)


def _ranked(
    readings: Sequence[Reading],
    *,
    settings: Settings,
    live: str,
    now_s: float,
    last: LastSwitch | None,
) -> list[tuple[int, str]]:
    ranked: list[tuple[int, str]] = []
    for reading in readings:
        fullness = _fullness(reading, settings=settings, live=live, now_s=now_s, last=last)
        if fullness is not None:
            ranked.append((fullness, reading.alias))
    # Percentages are compared across accounts only here, and only the worse of the two
    # rows that matter; ties go to the alias that sorts first.
    return sorted(ranked)


def next_candidate(
    readings: Sequence[Reading],
    *,
    settings: Settings,
    live: str,
    now_s: float,
    last: LastSwitch | None,
) -> str | None:
    """Where a switch would go if one were due, trigger or not."""
    ranked = _ranked(readings, settings=settings, live=live, now_s=now_s, last=last)
    return ranked[0][1] if ranked else None


def decide(
    readings: Sequence[Reading],
    *,
    settings: Settings,
    live: str | None,
    now_s: float,
    last: LastSwitch | None,
) -> Decision:
    if not settings.auto_switch:
        return _stay(REASON_OFF)
    if live is None:
        return _stay(REASON_LIVE_UNKNOWN)
    current = next((reading for reading in readings if reading.alias == live), None)
    if current is None or current.rows is None:
        return _stay(f"no fresh numbers for {live}")
    if not _over(current.rows, settings.threshold):
        return _stay(f"{live} is below {settings.threshold}%")
    blocked = blocking(current.rows, settings.threshold, now_s)
    if not blocked:
        return _stay(f"no fresh numbers for {live}")
    # abs: a few seconds of clock skew still counts as just switched, while a record that
    # names a time far ahead (a hand edit, a wrong clock) is no cooldown rather than one
    # that lasts until then.
    if last is not None and abs(now_s - last.at_s) < settings.cooldown_s:
        return _stay(f"cooldown: last switch {int(abs(now_s - last.at_s))}s ago")
    ranked = _ranked(readings, settings=settings, live=live, now_s=now_s, last=last)
    if not ranked:
        return _stay("no account with headroom")
    fullness, target = ranked[0]
    worst = max(blocked, key=lambda row: row.percent or 0)
    return Decision(
        "switch", target, f"{live} {worst.kind} at {worst.percent}%; {target} is at {fullness}%"
    )
