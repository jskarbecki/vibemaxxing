"""`pool.decide`: when to leave the live account, and for which one.

Pure, so every case is a table row: the readings, the settings, who is live, the
clock and the last switch go in; one Decision comes out. The one-line reason is
what `vibe auto tick` prints, so the tests pin the stay reasons too.
"""

from __future__ import annotations

from datetime import UTC, datetime

from vibemaxxing.models import AccountState
from vibemaxxing.poll import USAGE_FRESH_S
from vibemaxxing.pool import LastSwitch, Reading, Settings, decide, next_candidate
from vibemaxxing.usage import Row

NOW = 1_800_000_000.0
HOUR = 3600.0
ON = Settings(auto_switch=True)


def _iso(at_s: float) -> str:
    return datetime.fromtimestamp(at_s, UTC).isoformat()


def rows(
    session: int | None = 0,
    weekly: int | None = 0,
    scoped: int | None = None,
    *,
    reset_in_s: float | None = HOUR,
    now: float = NOW,
) -> tuple[Row, ...]:
    stamp = None if reset_in_s is None else _iso(now + reset_in_s)
    found = [("session", "Session", session), ("weekly_all", "Weekly", weekly)]
    found.append(("weekly_scoped", "Weekly · Fable", scoped))
    return tuple(
        Row(kind=kind, label=label, percent=percent, severity="normal", resets_at=stamp)
        for kind, label, percent in found
        if percent is not None or kind != "weekly_scoped"
    )


def view(
    alias: str,
    session: int | None = 0,
    weekly: int | None = 0,
    *,
    scoped: int | None = None,
    age_s: float = 10.0,
    state: AccountState = AccountState.OK,
    reset_in_s: float | None = HOUR,
    now: float = NOW,
) -> Reading:
    # `now` is the moment the reading was taken against: the age and the reset time
    # are both measured from it, so a test that moves the clock moves them together.
    return Reading(
        alias=alias,
        state=state,
        rows=rows(session, weekly, scoped, reset_in_s=reset_in_s, now=now),
        updated_at=now - age_s,
    )


def no_numbers(alias: str) -> Reading:
    return Reading(alias=alias, state=AccountState.OK, rows=None, updated_at=None)


def test_decide_picks_most_headroom() -> None:
    decision = decide(
        [view("a", session=99), view("b", session=40), view("c", session=10)],
        settings=ON,
        live="a",
        now_s=NOW,
        last=None,
    )

    assert decision.action == "switch"
    assert decision.target == "c"
    assert "a session at 99%" in decision.reason
    assert "c is at 10%" in decision.reason


def test_decide_ranks_by_the_worse_of_session_and_weekly() -> None:
    # c has the lower session, but its week is nearly gone: b leaves more room.
    decision = decide(
        [view("a", session=99), view("b", session=30, weekly=30), view("c", session=5, weekly=80)],
        settings=ON,
        live="a",
        now_s=NOW,
        last=None,
    )

    assert decision.target == "b"


def test_decide_below_threshold_stays() -> None:
    decision = decide(
        [view("a", session=98, weekly=98), view("b")], settings=ON, live="a", now_s=NOW, last=None
    )

    assert (decision.action, decision.target) == ("stay", None)
    assert decision.reason == "a is below 99%"


def test_decide_ignores_weekly_scoped() -> None:
    decision = decide(
        [view("a", session=5, weekly=5, scoped=100), view("b")],
        settings=ON,
        live="a",
        now_s=NOW,
        last=None,
    )

    assert decision.action == "stay"


def test_decide_a_trigger_on_either_row_counts() -> None:
    weekly = decide(
        [view("a", session=5, weekly=99), view("b")], settings=ON, live="a", now_s=NOW, last=None
    )
    session = decide(
        [view("a", session=100, weekly=5), view("b")], settings=ON, live="a", now_s=NOW, last=None
    )

    assert (weekly.target, session.target) == ("b", "b")


def test_decide_uses_the_configured_threshold() -> None:
    settings = Settings(auto_switch=True, threshold=90)

    decision = decide(
        [view("a", session=92), view("b", session=95), view("c", session=50)],
        settings=settings,
        live="a",
        now_s=NOW,
        last=None,
    )

    # b is over 90 as well, so only c is a place to go.
    assert decision.target == "c"


def test_decide_skips_a_candidate_within_the_margin_of_the_threshold() -> None:
    near = decide(
        [view("a", session=99), view("b", session=94)], settings=ON, live="a", now_s=NOW, last=None
    )
    clear = decide(
        [view("a", session=99), view("b", session=93)], settings=ON, live="a", now_s=NOW, last=None
    )

    assert (near.action, near.reason) == ("stay", "no account with headroom")
    assert clear.target == "b"


def test_decide_no_candidate() -> None:
    decision = decide(
        [view("a", session=99), view("b", weekly=99), view("c", session=100)],
        settings=ON,
        live="a",
        now_s=NOW,
        last=None,
    )

    assert (decision.action, decision.target) == ("stay", None)
    assert decision.reason == "no account with headroom"


def test_decide_with_one_account_has_nowhere_to_go() -> None:
    decision = decide([view("a", session=100)], settings=ON, live="a", now_s=NOW, last=None)

    assert decision.reason == "no account with headroom"


def test_decide_skips_stale_candidate() -> None:
    # 200 s is past USAGE_FRESH_S: an old reading of b says nothing about b now.
    assert USAGE_FRESH_S < 200.0
    decision = decide(
        [view("a", session=99), view("b", session=1, age_s=200.0)],
        settings=ON,
        live="a",
        now_s=NOW,
        last=None,
    )

    assert decision.action == "stay"
    assert decision.reason == "no account with headroom"


def test_decide_a_reading_from_the_future_is_not_fresh() -> None:
    decision = decide(
        [view("a", session=99), view("b", session=1, age_s=-30.0)],
        settings=ON,
        live="a",
        now_s=NOW,
        last=None,
    )

    assert decision.action == "stay"


def test_decide_respects_cooldown() -> None:
    last = LastSwitch("x", "a", at_s=NOW - 100.0, reason="earlier", until_s=None)

    decision = decide(
        [view("a", session=100), view("b")], settings=ON, live="a", now_s=NOW, last=last
    )

    assert decision.action == "stay"
    assert decision.reason.startswith("cooldown")
    later = NOW + 800.0
    after = decide(
        [view("a", session=100, now=later), view("b", now=later)],
        settings=ON,
        live="a",
        now_s=later,
        last=last,
    )
    assert after.target == "b", "900 s after the switch the cooldown is over"


def test_decide_a_record_from_far_in_the_future_is_no_cooldown() -> None:
    # A hand-edited or mis-clocked record must not hold auto-switch off until the year it
    # names; a few seconds of clock skew still counts as just switched.
    future = LastSwitch("x", "a", at_s=NOW + 10 * 86_400.0, reason="r", until_s=None)
    skewed = LastSwitch("x", "a", at_s=NOW + 30.0, reason="r", until_s=None)

    far = decide([view("a", session=100), view("b")], settings=ON, live="a", now_s=NOW, last=future)
    near = decide(
        [view("a", session=100), view("b")], settings=ON, live="a", now_s=NOW, last=skewed
    )

    assert far.target == "b"
    assert near.reason.startswith("cooldown")


def test_decide_never_returns_to_limited_account() -> None:
    # a was left while its week was at 99% and resets in two days. c is now at 99%.
    limited_until = NOW + 2 * 86_400.0
    last = LastSwitch(
        "a", "c", at_s=NOW - 5_000.0, reason="a weekly_all at 99%", until_s=limited_until
    )

    no_answer = decide(
        [no_numbers("a"), view("c", weekly=99), view("b", session=99)],
        settings=ON,
        live="c",
        now_s=NOW,
        last=last,
    )
    looks_fine = decide(
        [view("a", session=1, weekly=1), view("c", weekly=99), view("b", session=99)],
        settings=ON,
        live="c",
        now_s=NOW,
        last=last,
    )
    reset = limited_until + 1.0
    after_reset = decide(
        [view("a", session=1, weekly=1, now=reset), view("c", weekly=99, now=reset)],
        settings=ON,
        live="c",
        now_s=reset,
        last=last,
    )

    assert no_answer.action == "stay" and no_answer.reason == "no account with headroom"
    assert looks_fine.action == "stay", "fresh numbers do not override the memory of why it left"
    assert after_reset.target == "a", "once its limiting row has reset it is a candidate again"


def test_decide_honours_excluded() -> None:
    settings = Settings(auto_switch=True, excluded=("b",))

    decision = decide(
        [view("a", session=99), view("b", session=1), view("c", session=60)],
        settings=settings,
        live="a",
        now_s=NOW,
        last=None,
    )

    assert decision.target == "c"
    only_b = decide(
        [view("a", session=99), view("b", session=1)],
        settings=settings,
        live="a",
        now_s=NOW,
        last=None,
    )
    assert only_b.action == "stay"


def test_decide_is_off_until_switched_on() -> None:
    decision = decide(
        [view("a", session=100), view("b")],
        settings=Settings(auto_switch=False),
        live="a",
        now_s=NOW,
        last=None,
    )

    assert (decision.action, decision.reason) == ("stay", "auto-switch is off")


def test_decide_does_not_guess_who_is_live() -> None:
    decision = decide(
        [view("a", session=100), view("b")], settings=ON, live=None, now_s=NOW, last=None
    )

    assert (decision.action, decision.reason) == ("stay", "live account unknown")


def test_decide_never_switches_on_numbers_it_does_not_have() -> None:
    nothing = decide([no_numbers("a"), view("b")], settings=ON, live="a", now_s=NOW, last=None)
    absent = decide([view("b")], settings=ON, live="a", now_s=NOW, last=None)

    assert nothing.reason == "no fresh numbers for a"
    assert absent.reason == "no fresh numbers for a"
    assert nothing.action == absent.action == "stay"


def test_decide_a_reading_that_predates_its_reset_is_not_a_trigger() -> None:
    # 99% with a reset that has already passed: the window rolled over since, so the
    # account is probably nowhere near 99% now.
    stale = view("a", session=99, reset_in_s=-60.0)

    decision = decide([stale, view("b")], settings=ON, live="a", now_s=NOW, last=None)

    assert decision.action == "stay"
    assert decision.reason == "no fresh numbers for a"


def test_decide_a_row_without_a_reset_time_still_counts() -> None:
    decision = decide(
        [view("a", session=99, reset_in_s=None), view("b", session=99, reset_in_s=None)],
        settings=ON,
        live="a",
        now_s=NOW,
        last=None,
    )

    assert decision.reason == "no account with headroom"


def test_decide_only_ok_accounts_with_known_percentages_are_candidates() -> None:
    dead = view("b", state=AccountState.NEEDS_LOGIN)
    broken = view("c", state=AccountState.ERROR)
    unknown = view("d", session=None, weekly=None)
    half = view("e", session=None, weekly=5)

    decision = decide(
        [view("a", session=99), dead, broken, unknown, half],
        settings=ON,
        live="a",
        now_s=NOW,
        last=None,
    )

    assert decision.reason == "no account with headroom"


def test_decide_never_picks_an_account_it_could_not_identify_afterwards() -> None:
    # An account stored without a uuid leaves ~/.claude.json naming the account just left,
    # so after landing on it vibe cannot tell who is live and the next resync is a guess.
    nameless = Reading("b", AccountState.OK, rows(0, 0), NOW - 10.0, identified=False)

    decision = decide(
        [view("a", session=99), nameless], settings=ON, live="a", now_s=NOW, last=None
    )

    assert decision.reason == "no account with headroom"
    assert next_candidate([nameless], settings=ON, live="a", now_s=NOW, last=None) is None


def test_decide_ties_break_by_alias() -> None:
    decision = decide(
        [view("a", session=99), view("c", session=20), view("b", session=20)],
        settings=ON,
        live="a",
        now_s=NOW,
        last=None,
    )

    assert decision.target == "b"


def test_next_candidate_ignores_the_trigger() -> None:
    # `vibe auto status` shows where a switch would go, even while nothing is limited.
    best = next_candidate(
        [view("a", session=10), view("b", session=40), view("c", session=5)],
        settings=Settings(auto_switch=False),
        live="a",
        now_s=NOW,
        last=None,
    )

    assert best == "c"
