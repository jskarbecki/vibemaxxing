"""The web dashboard: four routes on loopback, and the page that renders them.

The server takes the envelope as a callable rather than a Context so the route
table can be tested without a store, a clock or a fake HTTP client. Everything
it writes goes through ``envelope.dumps``, which scrubs, so a token that reached
the payload by some route nobody anticipated still does not leave the process.

No authentication and no TLS: both are out of scope, and both are exactly why
the bind is ``127.0.0.1`` by construction rather than by a checked option.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import TYPE_CHECKING, Final

from vibemaxxing.envelope import build, collect, dumps, error_payload
from vibemaxxing.errors import VibeError
from vibemaxxing.redact import err

if TYPE_CHECKING:  # cli imports web, so the Context type may only travel one way.
    from vibemaxxing.cli import Context

LOOPBACK: Final = "127.0.0.1"
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


def snapshot(ctx: Context) -> dict[str, object]:
    # A live clock, not ctx.now_s: this process stays up for days, and a frozen
    # timestamp would evaluate every token's expiry against process start.
    now_s = time.time()
    return build(collect(ctx.root, client=ctx.client, now_s=now_s), now_s=now_s)


def serve(ctx: Context, *, port: int) -> int:
    server = build_server(port, envelope=lambda: snapshot(ctx))
    with server:
        err(f"dashboard on http://{LOOPBACK}:{server.server_address[1]} — ctrl-c to stop\n")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            err("\nstopped\n")
    return 0
