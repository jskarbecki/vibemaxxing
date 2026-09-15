"""The Textual dashboard: what it paints, and that it does not grow (AC14)."""

from __future__ import annotations

import gc
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path

from textual.content import Content
from textual.widgets import Static

from tests.fakes import FakeHttpClient, FakeKeychain, fixture_usage, make_credential, seed_account
from vibemaxxing import cli, tui
from vibemaxxing.credentials import EMPTY_IDENTITY, Identity
from vibemaxxing.envelope import AccountView
from vibemaxxing.models import AccountState
from vibemaxxing.usage import Row, Summary

NOW_S = 1_757_930_000.0
SENTINEL = "VMXTUISENTINEL000000"


def _iso(offset_s: float) -> str:
    return datetime.fromtimestamp(NOW_S + offset_s, UTC).isoformat()


def _row(
    kind: str,
    *,
    percent: int | None = 10,
    severity: str | None = "normal",
    offset_s: float = 7200.0,
) -> Row:
    return Row(
        kind=kind,
        label=kind.replace("_", " ").capitalize(),
        percent=percent,
        severity=severity,
        resets_at=_iso(offset_s),
    )


def _view(
    *,
    alias: str = "work",
    rows: tuple[Row, ...] = (),
    summary: Summary | None = None,
    state: AccountState = AccountState.OK,
    message: str | None = None,
    identity: Identity = EMPTY_IDENTITY,
    plan: str | None = "max",
) -> AccountView:
    return AccountView(
        alias=alias,
        active=True,
        state=state,
        message=message,
        identity=identity,
        plan=plan,
        summary=Summary(rows=rows, breakdown=()) if summary is None else summary,
        updated_at=NOW_S,
    )


def _text(widget: Static) -> str:
    visual = widget.visual
    assert isinstance(visual, Content)
    return visual.plain


def _context(root: Path, client: FakeHttpClient) -> cli.Context:
    return cli.Context(root=root, client=client, port=FakeKeychain(), now_s=NOW_S)


def _queue(client: FakeHttpClient, count: int) -> None:
    for _ in range(count):
        client.queue_json(200, fixture_usage())


def test_account_content_renders_every_row_from_the_payload() -> None:
    rows = tuple(_row(f"kind_{n}", percent=n * 20) for n in range(5))
    text = tui.account_content(_view(rows=rows), now_s=NOW_S).plain

    for row in rows:
        assert row.label in text
    assert "0%" in text
    assert "80%" in text
    assert "in 2h" in text


def test_severity_styles_every_elevated_row_and_no_calm_one() -> None:
    rows = (
        _row("session", severity="normal"),
        _row("weekly_all", severity="warning"),
        _row("weekly_scoped", severity="unheard_of"),
    )
    content = tui.account_content(_view(rows=rows), now_s=NOW_S)
    styled = " ".join(content.plain[span.start : span.end] for span in content.spans if span.style)

    assert "Weekly all" in styled
    assert "Weekly scoped" in styled
    assert "Session" not in styled


def test_needs_login_message_is_rendered_verbatim() -> None:
    message = 'the login for "old" has lapsed — run: vibe add old'
    text = tui.account_content(
        _view(
            alias="old",
            summary=Summary(rows=(), breakdown=()),
            state=AccountState.NEEDS_LOGIN,
            message=message,
        ),
        now_s=NOW_S,
    ).plain

    assert message in text


def test_who_falls_back_from_email_to_display_name_to_alias() -> None:
    both = Identity("jan@intra-ai.de", None, None, None, None, None, "Jan")
    named = Identity(None, None, None, None, None, None, "Jan")

    assert "jan@intra-ai.de" in tui.account_content(_view(identity=both), now_s=NOW_S).plain
    assert "Jan" in tui.account_content(_view(identity=named), now_s=NOW_S).plain
    assert "work" in tui.account_content(_view(identity=EMPTY_IDENTITY), now_s=NOW_S).plain


def test_footer_shows_pool_accounts_and_forecast() -> None:
    with_forecast = tui.footer_content(weeks=2.58, accounts=5, forecast=timedelta(hours=5)).plain
    without = tui.footer_content(weeks=2.58, accounts=5, forecast=None).plain

    assert "2.58" in with_forecast
    assert "5 accounts" in with_forecast
    assert "5.0h" in with_forecast
    assert "dry" not in without


async def test_refresh_paints_each_account_and_never_a_token(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"
    seed_account(root, "one", credential=make_credential(access=SENTINEL), active=True)
    seed_account(root, "two")
    client = FakeHttpClient()
    ctx = _context(root, client)
    _queue(client, 2)

    app = tui.Dashboard(ctx)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert set(app.panels) == {"one", "two"}
        painted = _text(app.panels["one"])
        assert "Weekly · all models" in painted
        assert SENTINEL not in painted
        footer = _text(app.query_one("#footer", Static))
        assert "0.88" in footer
        assert "2 accounts" in footer

        widgets = len(app.query("*"))
        _queue(client, 2)
        ctx.now_s = NOW_S + 60.0
        await app.refresh_cycle()
        await pilot.pause()

        assert len(app.query("*")) == widgets


async def test_dashboard_refresh_has_no_unbounded_growth(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"
    for n in range(5):
        seed_account(root, f"acct{n}", active=n == 0)
    client = FakeHttpClient()
    ctx = _context(root, client)
    _queue(client, 5)

    marks: dict[int, tuple[int, int]] = {}
    app = tui.Dashboard(ctx)
    async with app.run_test() as pilot:
        tracemalloc.start()
        for cycle in range(1, 201):
            # The fake's queue and request log are the test double's own memory,
            # not the dashboard's; reset them so the measurement is of the app.
            client.requests.clear()
            client.responses.clear()
            _queue(client, 5)
            ctx.now_s = NOW_S + cycle * 60.0
            await app.refresh_cycle()
            await pilot.pause()
            if cycle in (50, 200):
                gc.collect()
                marks[cycle] = (tracemalloc.get_traced_memory()[0], len(app.query("*")))
        tracemalloc.stop()

    assert marks[200][1] == marks[50][1]
    assert marks[200][0] - marks[50][0] < 1_000_000
