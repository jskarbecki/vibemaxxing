#!/usr/bin/env python3
"""Serve the dashboard over a fixed, fake envelope.

The README screenshot has to show a populated dashboard, and a real one shows
real addresses. This serves the same ``index.html`` the package ships against
invented accounts, so the picture can be retaken without putting anyone's email
in the repository.

    uv run python scripts/demo_dashboard.py     # http://127.0.0.1:8799

Not imported by the package and not covered by the suite: it exists to make one
image reproducible.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from vibemaxxing.web import page

PORT = 8799


def _at(hours: float) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat(timespec="seconds")


def _account(
    alias: str,
    email: str,
    plan: str,
    *,
    active: bool,
    session: int,
    weekly: int,
    scoped: int,
    session_in: float,
    weekly_in: float,
) -> dict[str, object]:
    def row(kind: str, label: str, percent: int, ahead: float) -> dict[str, object]:
        return {
            "kind": kind,
            "label": label,
            "percent": percent,
            "severity": "critical" if percent >= 90 else "normal",
            "resets_at": _at(ahead),
        }

    return {
        "alias": alias,
        "active": active,
        "state": "ok",
        "message": None,
        "email": email,
        "display_name": None,
        "organization": None,
        "plan": plan,
        "updated_at": _at(0),
        "rows": [
            row("session", "Session", session, session_in),
            row("weekly_all", "Weekly · all models", weekly, weekly_in),
            row("weekly_scoped", "Weekly · Fable", scoped, weekly_in),
        ],
        "breakdown": [
            {"label": "Claude Code", "percent": 96},
            {"label": "Chats", "percent": 3},
            {"label": "Cowork", "percent": 0},
            {"label": "Other", "percent": 1},
        ],
    }


def envelope() -> dict[str, object]:
    accounts = [
        _account(
            "work",
            "you@example.com",
            "max 20x",
            active=True,
            session=34,
            weekly=61,
            scoped=12,
            session_in=2.4,
            weekly_in=57,
        ),
        _account(
            "side",
            "side@example.com",
            "max",
            active=False,
            session=2,
            weekly=9,
            scoped=0,
            session_in=4.8,
            weekly_in=112,
        ),
        _account(
            "team",
            "team@example.com",
            "pro",
            active=False,
            session=71,
            weekly=94,
            scoped=40,
            session_in=1.1,
            weekly_in=20,
        ),
    ]
    return {
        "schema": 1,
        "generated_at": _at(0),
        "accounts": accounts,
        "pool": {"accounts": 3, "remaining_account_weeks": 1.36, "dry_in_seconds": None},
        "resets": [
            {"alias": "team", "at": _at(20)},
            {"alias": "work", "at": _at(57)},
            {"alias": "side", "at": _at(112)},
        ],
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, content_type: str, body: str) -> None:
        payload = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path == "/":
            self._send("text/html; charset=utf-8", page())
        elif self.path == "/api/usage":
            self._send("application/json", json.dumps(envelope(), indent=2))
        else:
            self.send_error(404)

    def log_message(self, format: str, *args: object) -> None:
        return


if __name__ == "__main__":
    with ThreadingHTTPServer(("127.0.0.1", PORT), Handler) as server:
        print(f"demo dashboard on http://127.0.0.1:{PORT}  (ctrl-c to stop)")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print()
