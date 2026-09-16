"""The web dashboard: four routes on loopback, and the page that renders them.

The server takes the envelope as a callable rather than a Context so the route
table can be tested without a store, a clock or a fake HTTP client. Everything
it writes goes through ``envelope.dumps``, which scrubs, so a token that reached
the payload by some route nobody anticipated still does not leave the process.

No authentication and no TLS: both are out of scope, and both are exactly why
the bind is ``127.0.0.1`` by construction rather than by a checked option.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Final

from vibemaxxing import history, store
from vibemaxxing.envelope import build, collect, dumps, error_payload, pool_weeks
from vibemaxxing.errors import VibeError
from vibemaxxing.poll import DASHBOARD_INTERVAL_S
from vibemaxxing.pool import dry_in
from vibemaxxing.redact import err

if TYPE_CHECKING:  # cli imports web, so the Context type may only travel one way.
    from vibemaxxing.cli import Context

LOOPBACK: Final = "127.0.0.1"
_WINDOW_S: Final = history.RETENTION_DAYS * 86_400.0
NOT_FOUND: Final = "not found\n"


def page() -> str:
    # Package data, not a path guessed from __file__: the page must resolve the
    # same way from the source tree, a wheel and a zip.
    return files("vibemaxxing").joinpath("index.html").read_text(encoding="utf-8")


def _failure(exc: Exception) -> VibeError:
    if isinstance(exc, VibeError):
        return exc
    # The type name and nothing else. An unclassified exception's message may
    # carry anything, and a traceback on a web page is a leak with a nice font.
    return VibeError(
        f"the dashboard could not build the usage snapshot ({type(exc).__name__})",
        "vibe usage --once",
    )


def build_server(port: int, *, envelope: Callable[[], dict[str, object]]) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, content_type: str, body: str) -> None:
            payload = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _usage(self) -> None:
            try:
                body, status = dumps(envelope()), 200
            except Exception as exc:
                body, status = dumps(error_payload(_failure(exc))), 500
            self._send(status, "application/json", body)

        def do_GET(self) -> None:
            if self.path == "/":
                self._send(200, "text/html; charset=utf-8", page())
            elif self.path == "/api/usage":
                self._usage()
            else:
                self._send(404, "text/plain; charset=utf-8", NOT_FOUND)

        def log_message(self, format: str, *args: object) -> None:
            # A dashboard polling every 60 s would otherwise scroll the terminal
            # it was started from for as long as the page is open.
            return

    return ThreadingHTTPServer((LOOPBACK, port), Handler)


class Recorder:
    """The one history connection this process owns.

    ThreadingHTTPServer answers each request on its own thread, so the connection
    is opened with sqlite's same-thread check off and every use is serialised
    here. The lock never covers the usage fetch — only the two sqlite calls.
    """

    def __init__(self, path: Path) -> None:
        self._conn = history.connect(path, check_same_thread=False)
        self._lock = threading.Lock()

    def record(self, *, at_s: float, weeks: float) -> timedelta | None:
        with self._lock:
            history.record(self._conn, at_s=at_s, pool=weeks)
            window = history.samples(self._conn, since_s=at_s - _WINDOW_S)
        return dry_in(window)

    def close(self) -> None:
        self._conn.close()


class _Cache:
    """One fetch per DASHBOARD_INTERVAL_S, however many tabs are open.

    Every browser polls /api/usage on its own timer and ThreadingHTTPServer
    answers each on its own thread, so without this the request rate is the
    number of open tabs times the page's poll rate -- the web dashboard had no
    floor at all.

    The lock is held across the fetch, which is what makes the docstring above
    true. Releasing it first let two tabs whose timers had drifted together both
    miss and both fetch: measured in history.db as sample pairs 10 ms apart from
    19:49 onward, which is 2x5 requests per cycle against a ~30/hour budget and
    the 429 that follows. A queued tab waits for the in-flight fetch and then
    re-checks freshness, so it is served that fetch's envelope instead of
    starting its own.
    """

    def __init__(self, ttl_s: float = DASHBOARD_INTERVAL_S) -> None:
        self._ttl_s = ttl_s
        self._lock = threading.Lock()
        self._at_s = -math.inf
        self._envelope: dict[str, object] | None = None

    def get(self, now_s: float, produce: Callable[[], dict[str, object]]) -> dict[str, object]:
        with self._lock:
            if self._envelope is not None and now_s - self._at_s < self._ttl_s:
                return self._envelope
            built = produce()
            self._envelope, self._at_s = built, now_s
            return built


def snapshot(ctx: Context, recorder: Recorder | None = None) -> dict[str, object]:
    # A live clock, not ctx.now_s: this process stays up for days, and a frozen
    # timestamp would evaluate every token's expiry against process start.
    now_s = ctx.clock()
    views = collect(ctx.root, client=ctx.client, now_s=now_s)
    # A cycle where an account failed to fetch reports a pool that is missing
    # that account's headroom. Recording it would store a transient 429 as a
    # genuine collapse and poison dry_in for as long as it stays in the window.
    complete = bool(views) and all(view.summary is not None for view in views)
    forecast = (
        recorder.record(at_s=now_s, weeks=pool_weeks(views))
        if recorder is not None and complete
        else None
    )
    return build(views, now_s=now_s, dry_in=forecast)


def serve(ctx: Context, *, port: int) -> int:
    recorder = Recorder(store.history_path(ctx.root))
    try:
        # Inside the guard: a bind failure here would otherwise leak the sqlite
        # connection and escape as a raw traceback.
        cache = _Cache()
        server = build_server(
            port,
            envelope=lambda: cache.get(ctx.clock(), lambda: snapshot(ctx, recorder)),
        )
        with server:
            err(f"dashboard on http://{LOOPBACK}:{server.server_address[1]} — ctrl-c to stop\n")
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                err("\nstopped\n")
    finally:
        recorder.close()
    return 0
