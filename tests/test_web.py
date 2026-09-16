"""The web dashboard: four routes, loopback only, and no token in any byte.

Every test drives a real socket on port 0 against 127.0.0.1. That is loopback,
not the network, so the autouse path guard leaves it alone.
"""

from __future__ import annotations

import http.client
import json
import re
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.fakes import (
    ENVELOPE_ACCOUNT_KEYS,
    FakeHttpClient,
    FakeKeychain,
    fixture_usage,
    make_credential,
    seed_account,
)
from vibemaxxing import cli, envelope, store, usage, web
from vibemaxxing.envelope import ENVELOPE_SCHEMA
from vibemaxxing.errors import VibeError
from vibemaxxing.httpclient import HTTPError
from vibemaxxing.models import AccountState
from vibemaxxing.poll import USAGE_FRESH_S
from vibemaxxing.redact import REDACTED, Secret

NOW_S = 1_757_930_000.0
SENTINEL = "VMXWEBSENTINEL0000000"
LOOPBACK = "127.0.0.1"


@contextmanager
def running(source: Callable[[], dict[str, object]]) -> Iterator[ThreadingHTTPServer]:
    server = web.build_server(0, envelope=source)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive(), "the server thread outlived the test"


def get(server: ThreadingHTTPServer, path: str) -> tuple[int, str, str]:
    conn = http.client.HTTPConnection(LOOPBACK, server.server_address[1], timeout=5)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        body = response.read().decode()
        return response.status, response.getheader("Content-Type") or "", body
    finally:
        conn.close()


def headers(server: ThreadingHTTPServer, path: str) -> dict[str, str]:
    conn = http.client.HTTPConnection(LOOPBACK, server.server_address[1], timeout=5)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        response.read()
        return {key.lower(): value for key, value in response.getheaders()}
    finally:
        conn.close()


def account_envelope(tmp_home: Path) -> dict[str, object]:
    root = tmp_home / ".vibemaxxing"
    seed_account(root, "work", active=True)
    client = FakeHttpClient()
    client.queue_json(200, fixture_usage())
    views = envelope.collect(root, client=client, now_s=NOW_S)
    return envelope.build(views, now_s=NOW_S)


def context(tmp_home: Path) -> cli.Context:
    return cli.Context(
        root=tmp_home / ".vibemaxxing",
        client=FakeHttpClient(),
        port=FakeKeychain(),
        now_s=NOW_S,
        prompt=lambda _: "",
        browser=lambda _: True,
    )


# --- AC13 --------------------------------------------------------------------


def test_web_binds_loopback_and_refuses_other_hosts(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with running(lambda: {"schema": ENVELOPE_SCHEMA}) as server:
        assert server.socket.getsockname()[0] == LOOPBACK

    assert cli.main(["usage", "web", "--host", "0.0.0.0"], context=context(tmp_home)) == 2
    assert "loopback" in capsys.readouterr().err


# --- the route table (contract section 11) -----------------------------------


def test_root_serves_the_page(tmp_home: Path) -> None:
    with running(lambda: account_envelope(tmp_home)) as server:
        status, content_type, body = get(server, "/")

    assert status == 200
    assert content_type == "text/html; charset=utf-8"
    assert "/api/usage" in body or "api/usage" in body
    # The page reads our endpoint, never the file the original dashboard read.
    assert ".credentials.json" not in body


def test_api_usage_serves_the_section_10_envelope(tmp_home: Path) -> None:
    expected = account_envelope(tmp_home)
    with running(lambda: expected) as server:
        status, content_type, body = get(server, "/api/usage")

    assert status == 200
    assert content_type == "application/json"
    payload = json.loads(body)
    assert payload == json.loads(envelope.dumps(expected))
    assert payload["schema"] == ENVELOPE_SCHEMA
    assert set(payload["pool"]) == {"accounts", "remaining_account_weeks", "dry_in_seconds"}
    assert set(payload["accounts"][0]) == ENVELOPE_ACCOUNT_KEYS


def test_unknown_path_is_404(tmp_home: Path) -> None:
    with running(lambda: account_envelope(tmp_home)) as server:
        status, content_type, body = get(server, "/nope")

    assert status == 404
    assert content_type == "text/plain; charset=utf-8"
    assert body == "not found\n"


def test_every_response_is_no_store(tmp_home: Path) -> None:
    with running(lambda: account_envelope(tmp_home)) as server:
        for path in ("/", "/api/usage", "/nope"):
            assert headers(server, path)["cache-control"] == "no-store"


# --- failures leak nothing ---------------------------------------------------


def test_a_failing_envelope_is_the_error_envelope(tmp_home: Path) -> None:
    Secret(SENTINEL)

    def boom() -> dict[str, object]:
        raise VibeError(f"the store is unreadable {SENTINEL}", "vibe list")

    with running(boom) as server:
        status, content_type, body = get(server, "/api/usage")

    assert status == 500
    assert content_type == "application/json"
    assert SENTINEL not in body
    assert "Traceback" not in body
    payload = json.loads(body)
    assert payload["schema"] == ENVELOPE_SCHEMA
    assert payload["error"]["code"] == "error"
    assert payload["error"]["recovery"] == "vibe list"
    assert REDACTED in payload["error"]["message"]


def test_an_unexpected_failure_is_still_a_clean_500(tmp_home: Path) -> None:
    Secret(SENTINEL)

    def boom() -> dict[str, object]:
        raise RuntimeError(f"unhandled {SENTINEL}")

    with running(boom) as server:
        status, _, body = get(server, "/api/usage")

    assert status == 500
    assert SENTINEL not in body
    assert "Traceback" not in body
    error = json.loads(body)["error"]
    assert error["recovery"] is not None
    assert "RuntimeError" in error["message"]


# --- the ported design properties --------------------------------------------


def test_the_page_keeps_the_ported_design_properties() -> None:
    page = web.page()

    assert "box-shadow" not in page
    # Ten hex literals, five light and five dark, all inside :root. Any colour
    # written anywhere else is the drift this asserts against.
    assert len(re.findall(r"#[0-9a-fA-F]{3,8}\b", page)) == 10
    assert "font-variant-numeric: tabular-nums" in page
    assert "prefers-color-scheme: dark" in page
    # Every other colour on the page is mixed from those ten, so the palette has
    # five decisions in it and both schemes stay in step by construction.
    assert page.count("color-mix(in oklab") >= 5
    # An em dash is not a number. The unknown-percent cell reads as a ledger nil.
    assert "\u2014" not in page
    # One motion in the whole page, plus its opt-out. Nothing else animates.
    assert page.count("transition:") == 2
    assert page.count("transition: width 240ms cubic-bezier(0.23, 1, 0.32, 1)") == 1
    assert page.count("transition: none") == 1
    assert "prefers-reduced-motion" in page


def test_the_page_is_a_ledger_beside_a_week() -> None:
    """The split is the design: accounts on the left, the week on the right, one
    column under 1080px. The timeline is inline SVG with no library, because the
    page is package data served over loopback with no network."""
    page = web.page()

    assert "grid-template-columns: minmax(0, 1fr) 1px 372px" in page
    assert "@media (max-width: 1080px)" in page
    assert "position: sticky" in page
    # No CDN, no bundle, no chart library: every byte the browser runs is here.
    assert "<script src" not in page
    assert "http://www.w3.org/2000/svg" in page
    for absent in ("cdn.", "unpkg", "jsdelivr", "//fonts."):
        assert absent not in page, absent
    # Every rendered time goes through toLocale*, which reads the machine's own
    # zone, and the page names that zone so the reader can see which one it used.
    assert "resolvedOptions().timeZone" in page
    assert "toLocaleDateString" in page and "toLocaleTimeString" in page


# --- the entry point the CLI calls -------------------------------------------


def test_snapshot_is_the_section_10_envelope(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"
    seed_account(root, "work", active=True)
    ctx = context(tmp_home)
    assert isinstance(ctx.client, FakeHttpClient)
    ctx.client.queue_json(200, fixture_usage())

    payload = web.snapshot(ctx)

    assert payload["schema"] == ENVELOPE_SCHEMA
    accounts = payload["accounts"]
    assert isinstance(accounts, list)
    assert set(accounts[0]) == ENVELOPE_ACCOUNT_KEYS
    assert accounts[0]["alias"] == "work"
    assert accounts[0]["active"] is True


def test_a_plan_less_account_backfills_its_plan_once(tmp_home: Path) -> None:
    """An account added by our own login has no subscriptionType, so the first
    fetch asks the profile endpoint and writes the answer to the account file."""
    root = tmp_home / ".vibemaxxing"
    seed_account(root, "work", credential=make_credential(subscription_type=None))
    client = FakeHttpClient()
    client.queue_json(
        200,
        {
            "organization": {
                "organization_type": "claude_max",
                "rate_limit_tier": "default_claude_max_20x",
            }
        },
    )
    client.queue_json(200, fixture_usage())

    views = envelope.collect(root, client=client, now_s=NOW_S)

    assert [view.plan for view in views] == ["max 20x"]
    stored = store.read_account(root, "work").credential
    assert (stored.subscription_type, stored.rate_limit_tier) == ("max", "default_claude_max_20x")

    # Second pass, once the cached answer has aged out: the plan is on disk now,
    # so only the usage request goes out.
    client.queue_json(200, fixture_usage())
    later = NOW_S + USAGE_FRESH_S
    assert [view.plan for view in envelope.collect(root, client=client, now_s=later)] == ["max 20x"]
    assert [request.url for request in client.requests].count(usage.PROFILE_URL) == 1
    assert client.responses == []


def test_a_failing_profile_endpoint_costs_only_the_plan(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"
    seed_account(root, "work", credential=make_credential(subscription_type=None))
    client = FakeHttpClient()
    client.queue(HTTPError(500, b"boom"))
    client.queue_json(200, fixture_usage())

    views = envelope.collect(root, client=client, now_s=NOW_S)

    assert views[0].plan is None
    assert views[0].state is AccountState.OK
    assert views[0].summary is not None


def test_two_simultaneous_tabs_cost_one_fetch() -> None:
    # Two tabs whose 60 s timers have drifted together hit /api/usage on two
    # threads at once. Both miss the TTL, and before the lock covered the fetch
    # both went on to make it: 2x5 requests per cycle against a ~30/hour budget,
    # which is the 429 the dashboard used to draw on itself.
    cache = web._Cache(ttl_s=180.0)
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def produce() -> dict[str, object]:
        nonlocal calls
        calls += 1
        entered.set()
        release.wait(timeout=10)
        return {"schema": ENVELOPE_SCHEMA}

    first = threading.Thread(target=lambda: cache.get(1000.0, produce))
    first.start()
    assert entered.wait(timeout=10)

    second = threading.Thread(target=lambda: cache.get(1000.0, produce))
    second.start()
    # The second tab must be queued behind the fetch, not inside it. Long enough
    # that a cache without the guard has entered produce and bumped the count.
    second.join(timeout=0.25)

    release.set()
    first.join(timeout=10)
    second.join(timeout=10)
    assert calls == 1
