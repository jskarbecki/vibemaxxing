"""The Textual dashboard: one group per account, one pooled footer.

The widget tree is built from the account *shape* — which aliases are on disk,
and what each one puts on its own lines — and rebuilt only when that changes.
Every refresh after that writes new values into the widgets that already exist,
because this is a window left open for days and a tree rebuilt every minute is a
leak with a nice render (AC14).

Colour comes from each row's ``severity``: ``"normal"`` is calm and everything
else is elevated. The set is open-ended, so no percentage threshold is invented
here and no row layout is hardcoded — the rows are whatever the payload carried.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, ClassVar, Final

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.content import Content
from textual.widgets import Static

from vibemaxxing import envelope, history, store
from vibemaxxing.envelope import AccountView
from vibemaxxing.poll import DASHBOARD_INTERVAL_S
from vibemaxxing.pool import dry_in
from vibemaxxing.redact import scrub
from vibemaxxing.usage import Row

if TYPE_CHECKING:  # cli imports tui; the annotation must not import it back.
    from vibemaxxing.cli import Context

# The floor is 60 s; the cadence is not. See poll.DASHBOARD_INTERVAL_S.
REFRESH_S: Final = DASHBOARD_INTERVAL_S
BAR_WIDTH: Final = 20
LABEL_WIDTH: Final = 22

_CALM: Final = ""
_ELEVATED: Final = "$warning"
_WINDOW_S: Final = history.RETENTION_DAYS * 86_400.0
_EMPTY: Final = "no accounts yet — run: vibe add"


def _relative(resets_at: str | None, now_s: float) -> str:
    if resets_at is None:
        return ""
    try:
        when = datetime.fromisoformat(resets_at)
    except ValueError:
        # A timestamp from the network, so its shape is not ours to trust.
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    left_s = when.timestamp() - now_s
    if left_s <= 0:
        return "resets now"
    if left_s < 3600:
        return f"resets in {round(left_s / 60)}m"
    if left_s < 86_400:
        return f"resets in {round(left_s / 3600)}h"
    return f"resets in {round(left_s / 86_400)}d"


def _bar(percent: int | None) -> str:
    if percent is None:
        return "─" * BAR_WIDTH
    filled = round(min(max(percent, 0), 100) / 100 * BAR_WIDTH)
    return "█" * filled + "░" * (BAR_WIDTH - filled)


def _row_line(row: Row, now_s: float) -> tuple[str, str]:
    percent = "—" if row.percent is None else f"{row.percent}%"
    line = (
        f"  {scrub(row.label):<{LABEL_WIDTH}}{_bar(row.percent)} "
        f"{percent:>4}  {_relative(row.resets_at, now_s)}"
    )
    return line, _CALM if row.severity in (None, "normal") else _ELEVATED


def _who(view: AccountView) -> str:
    return view.identity.email or view.identity.display_name or view.alias


def account_content(view: AccountView, *, now_s: float) -> Content:
    lines: list[Content] = [
        Content.assemble(
            ("● " if view.active else "  ", _CALM),
            (scrub(view.alias), "bold"),
            (f"  {scrub(_who(view))}  {scrub(view.plan or '—')}", "dim"),
        )
    ]
    if view.message is not None:
        # Already carries its literal recovery command; never recomposed here.
        lines.append(Content.styled(f"  {scrub(view.message)}", _ELEVATED))
    summary = view.summary
    if summary is not None and not summary.rows:
        lines.append(Content.styled("  no limits reported", "dim"))
    for row in summary.rows if summary is not None else ():
        line, style = _row_line(row, now_s)
        lines.append(Content.styled(line, style))
    return Content("\n").join(lines)


def _shape(view: AccountView) -> tuple[str, bool, tuple[str, ...] | None]:
    """What a panel puts on its own lines: any change here needs a new layout."""
    summary = view.summary
    return (
        view.alias,
        view.message is not None,
        None if summary is None else tuple(row.label for row in summary.rows),
    )


def footer_content(*, weeks: float, accounts: int, forecast: timedelta | None) -> Content:
    text = f"pool {weeks} account-weeks  ·  {accounts} accounts"
    if forecast is not None:
        text += f"  ·  dry in {forecast.total_seconds() / 3600:.1f}h"
    return Content(text)


class Dashboard(App[None]):
    CSS = """
    #accounts { height: 1fr; padding: 1 2; }
    #empty { display: none; }
    #footer { dock: bottom; height: 1; padding: 0 2; background: $panel; }
    .account { margin-bottom: 1; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit", "quit"),
        Binding("r", "refresh_now", "refresh"),
    ]

    def __init__(self, ctx: Context) -> None:
        super().__init__()
        self._ctx = ctx
        self.panels: dict[str, Static] = {}
        # None until the first cycle lands: an empty store and a store nobody
        # has read yet say different things, and only one of them is "vibe add".
        self._shapes: list[tuple[str, bool, tuple[str, ...] | None]] | None = None
        # None until on_mount succeeds: a failure to open it must not turn every
        # later error into an AttributeError that replaces the real cause.
        self._conn: sqlite3.Connection | None = None
        # At most one fan-out in flight. Textual's message queue is unbounded, so
        # a held `r` would otherwise queue an arbitrary number of complete
        # network fan-outs and the account would draw 429s from its own UI.
        self._busy = False
        # Our own executor, not asyncio's shared default: its threads are
        # non-daemon and asyncio.run joins them at teardown, so `q` would leave
        # the process alive and unresponsive for the rest of an in-flight fetch.
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vibe-collect")
        # Set once the first cycle has painted. The first refresh is deliberately
        # off the message pump, so "mounted" and "showing something" are two
        # different moments and a caller that needs the second must wait for it.
        self.first_paint = asyncio.Event()

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="accounts"):
            yield Static(_EMPTY, id="empty")
        yield Static(id="footer")

    async def on_mount(self) -> None:
        self._conn = history.connect(store.history_path(self._ctx.root))
        # The timer first, then the first fetch off the message pump: awaiting the
        # whole fan-out here leaves a blank window that queues every keystroke,
        # `q` and ctrl-c included, for as long as the slowest account takes.
        self.set_interval(REFRESH_S, self._tick)
        self.call_after_refresh(self._tick)

    def on_unmount(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
        if self._conn is not None:
            self._conn.close()

    async def _tick(self) -> None:
        # The only clock read. refresh_cycle takes its time from the Context, so a
        # test drives it as fast as it likes without sleeping.
        self._ctx.now_s = self._ctx.clock()
        await self._refresh_or_report()

    async def _refresh_or_report(self) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            await self.refresh_cycle()
        except Exception as exc:
            # The window stays up for days and the usage endpoint is documented
            # to answer 429 under sustained polling, so one failed poll reports
            # itself and the next one is 60 s away. Anything collect() already
            # classifies arrives as a per-account message instead.
            self.query_one("#footer", Static).update(
                Content(scrub(f"refresh failed ({type(exc).__name__}) — run: vibe list")),
                layout=False,
            )
        finally:
            self._busy = False
            self.first_paint.set()

    async def action_refresh_now(self) -> None:
        await self._tick()

    async def refresh_cycle(self) -> None:
        ctx = self._ctx
        now_s = ctx.now_s
        # collect() refreshes tokens and fetches usage: off the event loop, or
        # the dashboard stops answering keys for the length of a timeout.
        views = await asyncio.get_running_loop().run_in_executor(
            self._pool, partial(envelope.collect, ctx.root, client=ctx.client, now_s=now_s)
        )
        weeks = envelope.pool_weeks(views)
        forecast = None
        if self._conn is not None:
            # A cycle where an account failed to fetch under-reports the pool;
            # storing it would read back as a real collapse and poison dry_in.
            if views and all(view.summary is not None for view in views):
                history.record(self._conn, at_s=now_s, pool=weeks)
            forecast = dry_in(history.samples(self._conn, since_s=now_s - _WINDOW_S))
        rebuilt = await self._reshape(views)
        # A panel's height follows its shape, so a cycle that only changes
        # values — a bar, a percent, a reset time — needs no layout pass.
        for view in views:
            self.panels[view.alias].update(account_content(view, now_s=now_s), layout=rebuilt)
        self.query_one("#footer", Static).update(
            footer_content(weeks=weeks, accounts=len(views), forecast=forecast), layout=False
        )

    async def _reshape(self, views: Sequence[AccountView]) -> bool:
        shapes = [_shape(view) for view in views]
        if shapes == self._shapes:
            return False
        self._shapes = shapes
        for panel in self.panels.values():
            await panel.remove()
        self.panels = {view.alias: Static(classes="account") for view in views}
        self.query_one("#empty", Static).display = not views
        await self.query_one("#accounts", VerticalScroll).mount_all(self.panels.values())
        return True


def run(ctx: Context) -> int:
    Dashboard(ctx).run()
    return 0
