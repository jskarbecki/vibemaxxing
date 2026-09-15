from __future__ import annotations

from datetime import timedelta

import pytest

from vibemaxxing.models import AccountState, Sample
from vibemaxxing.pool import AccountUsage, dry_in, pool_remaining
from vibemaxxing.usage import Row


def account(state: AccountState, percent: int | None) -> AccountUsage:
    rows = (
        Row(kind="session", label="Session", percent=0, severity="normal", resets_at=None),
        Row(
            kind="weekly_all",
            label="Weekly · all models",
            percent=percent,
            severity="normal",
            resets_at=None,
        ),
    )
    return AccountUsage(state=state, rows=rows)


def test_pool_remaining_sums_weekly_all_headroom() -> None:
    accounts = [account(AccountState.OK, p) for p in (0, 56, 66, 100, 20)]

    assert pool_remaining(accounts) == pytest.approx(2.58, abs=0.005)

    dead = [
        *accounts,
        account(AccountState.NEEDS_LOGIN, 0),
        account(AccountState.ERROR, 0),
    ]

    assert pool_remaining(dead) == pytest.approx(2.58, abs=0.005)
    assert len(dead) == 7


def test_pool_remaining_ignores_accounts_without_a_weekly_all_row() -> None:
    only_session = AccountUsage(
        state=AccountState.OK,
        rows=(Row(kind="session", label="Session", percent=0, severity=None, resets_at=None),),
    )

    assert pool_remaining([only_session]) == 0.0
    assert pool_remaining([account(AccountState.OK, None)]) == 0.0
    assert pool_remaining([]) == 0.0


def test_pool_remaining_clamps_percent_into_range() -> None:
    assert pool_remaining([account(AccountState.OK, 140)]) == 0.0
    assert pool_remaining([account(AccountState.OK, -20)]) == 1.0


def test_burn_forecast_measured_from_newest_sample() -> None:
    samples = [Sample(at_s=0.0, pool=3.0), Sample(at_s=3600.0, pool=2.5)]

    assert dry_in(samples) == timedelta(hours=5)

    assert dry_in([Sample(0.0, 3.0), Sample(3600.0, 3.0)]) is None
    assert dry_in([Sample(0.0, 2.5), Sample(3600.0, 3.0)]) is None
    assert dry_in([Sample(0.0, 3.0)]) is None
    assert dry_in([]) is None
    assert dry_in([Sample(10.0, 3.0), Sample(10.0, 2.5)]) is None


def test_burn_forecast_is_not_clamped_at_a_reset_boundary() -> None:
    # Ten account-weeks draining at one an hour forecasts ten hours, not "until the
    # weekly reset". A forecast that stops at the boundary lies.
    samples = [Sample(at_s=0.0, pool=11.0), Sample(at_s=3600.0, pool=10.0)]

    assert dry_in(samples) == timedelta(hours=10)
