"""Poll slice: the pure planner. Every test drives it from a simulated clock."""

from __future__ import annotations

from vibemaxxing.poll import MIN_GAP_S, POLL_FLOOR_S, Due, Scheduler


def test_poll_scheduler_respects_floor_stagger_and_backoff() -> None:
    aliases = ["a", "b", "c", "d", "e"]
    scheduler = Scheduler(aliases, start_s=0.0)
    latency_s = 7.0
    issued: list[Due] = []
    in_flight: list[tuple[float, str]] = []

    for tick in range(600):  # ten simulated minutes, one tick per second
        now = float(tick)
        for done_s, alias in [item for item in in_flight if item[0] <= now]:
            if alias == "c":
                scheduler.record_error(alias, now_s=done_s)
            else:
                scheduler.record_success(alias, now_s=done_s)
        in_flight = [item for item in in_flight if item[0] > now]

        due = scheduler.next_due(now_s=now)
        if due is not None:
            issued.append(due)
            in_flight.append((now + latency_s, due.alias))

    scheduled = [due for due in issued if not due.backoff]
    retries = [due for due in issued if due.backoff]

    windows = [sum(1 for due in scheduled if t <= due.at_s < t + 60.0) for t in range(600)]
    assert max(windows) == 5

    def gaps(dues: list[Due]) -> list[float]:
        return [b.at_s - a.at_s for a, b in zip(dues, dues[1:], strict=False)]

    assert min(gaps(scheduled)) >= MIN_GAP_S
    assert min(gaps(issued)) >= MIN_GAP_S

    for alias in aliases:
        polls = [due for due in issued if due.alias == alias]
        assert min(gaps(polls)) >= POLL_FLOOR_S

    first_error_s = next(due.at_s for due in issued if due.alias == "c") + latency_s
    assert {due.alias for due in retries} == {"c"}
    assert [due.at_s - first_error_s for due in retries] == [60.0, 120.0, 240.0, 480.0]


def test_poll_backoff_holds_at_the_last_step() -> None:
    scheduler = Scheduler(["a"], start_s=0.0)
    fired: list[float] = []

    for tick in range(3000):
        now = float(tick)
        if scheduler.next_due(now_s=now) is not None:
            fired.append(now)
            scheduler.record_error("a", now_s=now)

    assert fired == [0.0, 60.0, 120.0, 240.0, 480.0, 960.0, 1440.0, 1920.0, 2400.0, 2880.0]


def test_poll_stagger_widens_when_accounts_exceed_the_gap() -> None:
    aliases = [f"a{i}" for i in range(12)]
    scheduler = Scheduler(aliases, start_s=0.0)
    issued: list[Due] = []

    for tick in range(240):
        now = float(tick)
        due = scheduler.next_due(now_s=now)
        if due is not None:
            issued.append(due)
            scheduler.record_success(due.alias, now_s=now)

    # 12 accounts cannot fit inside POLL_FLOOR_S at MIN_GAP_S, so the cycle lengthens.
    assert [due.at_s for due in issued[:12]] == [float(i * 10) for i in range(12)]
    assert [due.at_s for due in issued[12:]] == [float(120 + i * 10) for i in range(12)]


def test_poll_exhausted_account_polls_at_its_reset() -> None:
    scheduler = Scheduler(["a"], start_s=0.0)
    assert scheduler.next_due(now_s=0.0) == Due("a", 0.0, backoff=False)

    scheduler.record_exhausted("a", now_s=0.0, resets_at_s=3600.0)

    assert scheduler.next_due(now_s=3599.0) is None
    assert scheduler.next_due(now_s=3600.0) == Due("a", 3600.0, backoff=False)


def test_poll_no_accounts_is_never_due() -> None:
    assert Scheduler([], start_s=0.0).next_due(now_s=1.0e9) is None
