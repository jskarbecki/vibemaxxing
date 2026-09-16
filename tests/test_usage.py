from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from tests.fakes import FakeHttpClient, fixture_usage, make_credential, seed_account
from vibemaxxing import envelope, store
from vibemaxxing.envelope import AccountView
from vibemaxxing.errors import NetworkError
from vibemaxxing.httpclient import HTTPError, HttpResponse, _retry_after_s
from vibemaxxing.models import AccountState
from vibemaxxing.poll import BACKOFF_S, USAGE_FRESH_S, USAGE_WAIT_CAP_S
from vibemaxxing.redact import Secret
from vibemaxxing.usage import (
    BETA_HEADER,
    PROFILE_URL,
    USAGE_URL,
    fetch_plan,
    fetch_usage,
    summarize,
)

FIXTURE = Path(__file__).parent / "fixture_usage.json"
SENTINEL_TOKEN = "sk-ant-oat01-VMXUSAGESENTINEL0000000000000000"


def fixture_payload() -> dict[str, object]:
    payload: dict[str, object] = json.loads(FIXTURE.read_text())
    return payload


def test_summarize_fixture_rows_and_labels() -> None:
    summary = summarize(fixture_payload())

    assert len(summary.rows) == 3
    assert [row.percent for row in summary.rows] == [0, 56, 66]
    assert [row.label for row in summary.rows] == [
        "Session",
        "Weekly · all models",
        "Weekly · Fable",
    ]
    assert [row.kind for row in summary.rows] == ["session", "weekly_all", "weekly_scoped"]
    assert summary.rows[1].resets_at == "2026-09-19T13:00:00.191007+00:00"
    assert summary.rows[1].severity == "normal"
    assert [(row.label, row.percent) for row in summary.breakdown] == [
        ("Claude Code", 100),
        ("Chats", 0),
        ("Cowork", 0),
        ("Other", 0),
    ]


def test_unknown_limit_kind_renders() -> None:
    summary = summarize({"limits": [{"kind": "monthly_experimental", "percent": 12}]})

    assert len(summary.rows) == 1
    row = summary.rows[0]
    assert row.kind == "monthly_experimental"
    assert row.label == "Monthly experimental"
    assert row.percent == 12
    assert row.severity is None
    assert row.resets_at is None


def test_summarize_reads_only_limits_and_breakdown() -> None:
    # The live response carried 22 top-level keys where the fixture has 9, and a
    # "warning" severity the fixture does not contain. Neither may change the rows.
    payload = fixture_payload()
    payload["amber_ladder"] = {"whatever": True}
    payload["tangelo"] = [1, 2, 3]
    payload["seven_day_opus"] = {"utilization": 12.0}
    payload["five_hour"] = {"utilization": 99.0}
    payload["seven_day"] = {"utilization": 99.0}
    limits = payload["limits"]
    assert isinstance(limits, list)
    first = limits[0]
    assert isinstance(first, dict)
    first["severity"] = "warning"
    breakdown = payload["seven_day_breakdown"]
    assert isinstance(breakdown, dict)
    breakdown["window_started_at"] = "2026-09-12T13:00:00+00:00"

    summary = summarize(payload)

    assert [row.percent for row in summary.rows] == [0, 56, 66]
    assert summary.rows[0].severity == "warning"
    assert len(summary.breakdown) == 4


def test_weekly_scoped_without_display_name_falls_through() -> None:
    summary = summarize({"limits": [{"kind": "weekly_scoped", "percent": 3, "scope": None}]})

    assert summary.rows[0].label == "Weekly scoped"


def test_summarize_tolerates_a_payload_without_limits() -> None:
    summary = summarize({})

    assert summary.rows == ()
    assert summary.breakdown == ()


def test_fetch_usage_sends_the_bearer_and_beta_header() -> None:
    client = FakeHttpClient()
    client.queue_json(200, fixture_payload())

    payload = fetch_usage(client, Secret(SENTINEL_TOKEN))

    assert payload["member_dashboard_available"] is False
    request = client.requests[0]
    assert request.method == "GET"
    assert request.url == USAGE_URL
    assert request.headers["Authorization"] == f"Bearer {SENTINEL_TOKEN}"
    assert request.headers["anthropic-beta"] == BETA_HEADER
    assert request.body is None


def test_token_appears_only_in_the_authorization_header() -> None:
    client = FakeHttpClient()
    client.queue_json(200, fixture_payload())

    payload = fetch_usage(client, Secret(SENTINEL_TOKEN))
    summary = summarize(payload)

    request = client.requests[0]
    assert SENTINEL_TOKEN not in request.url
    assert SENTINEL_TOKEN not in str(request.body)
    elsewhere = [
        name
        for name, value in request.headers.items()
        if name != "Authorization" and SENTINEL_TOKEN in value
    ]
    assert elsewhere == []
    assert SENTINEL_TOKEN not in repr(payload)
    assert SENTINEL_TOKEN not in json.dumps(payload)
    assert SENTINEL_TOKEN not in repr(summary)


def test_fetch_usage_rejects_a_body_that_is_not_json() -> None:
    client = FakeHttpClient()
    client.queue(HttpResponse(200, b"<html>maintenance</html>"))

    with pytest.raises(NetworkError):
        fetch_usage(client, Secret(SENTINEL_TOKEN))


# The live profile response, trimmed to the two members that answer "which plan".
PROFILE_PAYLOAD = {
    "account": {"email": "jan@intra-ai.de", "has_claude_max": True},
    "organization": {
        "organization_type": "claude_max",
        "rate_limit_tier": "default_claude_max_20x",
        "seat_tier": None,
    },
    "application": {"slug": "claude-code"},
}


def test_fetch_plan_reads_the_organization_type_and_tier() -> None:
    client = FakeHttpClient()
    client.queue_json(200, PROFILE_PAYLOAD)

    assert fetch_plan(client, Secret(SENTINEL_TOKEN)) == ("max", "default_claude_max_20x")

    request = client.requests[0]
    assert request.url == PROFILE_URL
    assert request.headers["Authorization"] == f"Bearer {SENTINEL_TOKEN}"
    assert SENTINEL_TOKEN not in request.url


def test_fetch_plan_tolerates_a_profile_without_an_organization() -> None:
    client = FakeHttpClient()
    client.queue_json(200, {"account": {"email": "jan@intra-ai.de"}})

    assert fetch_plan(client, Secret(SENTINEL_TOKEN)) == (None, None)


def _one(root: Path, client: FakeHttpClient, now_s: float) -> AccountView:
    (view,) = envelope.collect(root, client=client, now_s=now_s)
    return view


def test_every_process_shares_one_fetch_per_interval(tmp_home: Path) -> None:
    # Two clients stand in for two processes -- a dashboard and a `vibe list`.
    # The budget is per identity, so the second must be served from disk.
    root = tmp_home / ".vibemaxxing"
    seed_account(root, "one")
    dashboard = FakeHttpClient()
    dashboard.queue_json(200, fixture_usage())
    first = _one(root, dashboard, 1000.0)

    cli = FakeHttpClient()
    second = _one(root, cli, 1000.0 + USAGE_FRESH_S - 1)

    assert cli.requests == []
    assert second.summary == first.summary
    assert second.updated_at == 1000.0
    assert second.message is None

    cli.queue_json(200, fixture_usage())
    third = _one(root, cli, 1000.0 + USAGE_FRESH_S)
    assert len(cli.requests) == 1
    assert third.updated_at == 1000.0 + USAGE_FRESH_S


def test_a_429_keeps_the_last_numbers_and_backs_off(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"
    seed_account(root, "one")
    client = FakeHttpClient()
    client.queue_json(200, fixture_usage())
    good = _one(root, client, 1000.0)

    client.queue(HTTPError(429, b""))
    limited_at = 1000.0 + USAGE_FRESH_S
    limited = _one(root, client, limited_at)
    assert limited.state is AccountState.OK
    assert limited.summary == good.summary
    assert limited.updated_at == 1000.0
    assert limited.message is not None
    assert "rate limited - next try" in limited.message
    assert "showing usage from" in limited.message

    # Inside the first rung of the ladder: no request goes out, same answer.
    before = len(client.requests)
    waiting = _one(root, client, limited_at + BACKOFF_S[0] - 1)
    assert len(client.requests) == before
    assert waiting.summary == good.summary
    assert waiting.message == limited.message

    # Past it the retry goes out; a second 429 climbs to the next rung.
    client.queue(HTTPError(429, b""))
    retried_at = limited_at + BACKOFF_S[0]
    _one(root, client, retried_at)
    assert len(client.requests) == before + 1
    cache = store.read_usage_cache(root, "one")
    assert cache is not None
    assert cache.failures == 2
    assert cache.retry_at == retried_at + BACKOFF_S[1]

    # A success clears the ladder and replaces the numbers.
    client.queue_json(200, fixture_usage())
    healed = _one(root, client, retried_at + BACKOFF_S[1])
    assert healed.message is None
    assert healed.updated_at == retried_at + BACKOFF_S[1]
    cache = store.read_usage_cache(root, "one")
    assert cache is not None
    assert (cache.failures, cache.retry_at) == (0, None)


def test_retry_after_longer_than_the_ladder_wins(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"
    seed_account(root, "one")
    client = FakeHttpClient()
    client.queue(HTTPError(429, b"", retry_after_s=900.0))

    view = _one(root, client, 1000.0)

    # Nothing to fall back on yet, so this one is an error -- but a waiting one.
    assert view.state is AccountState.ERROR
    assert view.summary is None
    cache = store.read_usage_cache(root, "one")
    assert cache is not None
    assert cache.retry_at == 1900.0
    assert _one(root, client, 1899.0).message == view.message
    assert len(client.requests) == 1


def test_a_network_failure_shows_the_last_numbers_without_backing_off(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"
    seed_account(root, "one")
    client = FakeHttpClient()
    client.queue_json(200, fixture_usage())
    _one(root, client, 1000.0)

    # Offline spends no budget, so the next cycle may try again straight away.
    client.queue(NetworkError("could not reach api.anthropic.com: TimeoutError", "vibe list"))
    offline = _one(root, client, 1000.0 + USAGE_FRESH_S)
    assert offline.state is AccountState.OK
    assert offline.summary is not None
    cache = store.read_usage_cache(root, "one")
    assert cache is not None
    assert cache.retry_at is None


def test_a_broken_cache_file_is_no_cache(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"
    seed_account(root, "one")
    path = store.usage_path(root, "one")
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    client = FakeHttpClient()
    client.queue_json(200, fixture_usage())

    assert _one(root, client, 1000.0).summary is not None
    assert len(client.requests) == 1
    assert path.stat().st_mode & 0o777 == 0o600


def test_removing_an_account_removes_its_cached_usage(tmp_home: Path) -> None:
    # A re-added alias may be a different login; it must not inherit these numbers.
    root = tmp_home / ".vibemaxxing"
    seed_account(root, "one")
    client = FakeHttpClient()
    client.queue_json(200, fixture_usage())
    _one(root, client, 1000.0)
    assert store.usage_path(root, "one").exists()

    store.delete_account(root, "one")

    assert not store.usage_path(root, "one").exists()


def test_a_login_re_added_under_the_same_alias_does_not_inherit_numbers(
    tmp_home: Path,
) -> None:
    root = tmp_home / ".vibemaxxing"
    seed_account(root, "work", added_at=100.0)
    client = FakeHttpClient()
    client.queue_json(200, fixture_usage())
    _one(root, client, 1000.0)

    # `vibe add work` onto the alias writes a new account with a new added_at and
    # leaves usage/work.json behind. A 429 on its first fetch must not show the
    # previous login's numbers as this one's.
    seed_account(root, "work", added_at=1001.0)
    client.queue(HTTPError(429, b""))
    view = _one(root, client, 1002.0)

    assert len(client.requests) == 2
    assert view.state is AccountState.ERROR
    assert view.summary is None


def test_absurd_times_from_the_server_or_the_file_never_fail_the_view(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"
    seed_account(root, "one", added_at=100.0)
    client = FakeHttpClient()

    assert _retry_after_s("inf") is None
    assert _retry_after_s("nan") is None
    client.queue(HTTPError(429, b"", retry_after_s=1e13))
    _one(root, client, 1000.0)
    cache = store.read_usage_cache(root, "one")
    assert cache is not None
    assert cache.retry_at == 1000.0 + USAGE_WAIT_CAP_S

    path = store.usage_path(root, "one")
    payload = json.dumps(fixture_usage())
    assert '"percent": 56' in payload
    infinite = payload.replace('"percent": 56', '"percent": 1e400')
    for edited in (
        f'"fetched_at": 1e20, "payload": {payload}',
        f'"fetched_at": 999.0, "retry_at": 1e20, "payload": {payload}',
        # json.loads reads 1e400 as inf, which round() cannot take.
        f'"fetched_at": 999.0, "payload": {infinite}',
    ):
        path.write_text('{"schema": 1, "added_at": 100.0, "failures": 0, ' + edited + "}")
        client.responses.clear()
        client.queue_json(200, fixture_usage())
        view = _one(root, client, 1000.0)
        assert view.summary is not None


def test_a_refused_token_is_an_error_not_frozen_numbers(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"
    seed_account(root, "one")
    client = FakeHttpClient()
    client.queue_json(200, fixture_usage())
    _one(root, client, 1000.0)

    client.queue(HTTPError(401, b""))
    view = _one(root, client, 1000.0 + USAGE_FRESH_S)

    assert view.state is AccountState.ERROR
    assert view.summary is None
    assert envelope.pool_weeks([view]) == 0.0


def test_an_answer_older_than_the_cap_is_not_shown(tmp_home: Path) -> None:
    # A laptop waking offline days later must not present last week's numbers.
    root = tmp_home / ".vibemaxxing"
    seed_account(root, "one")
    client = FakeHttpClient()
    client.queue_json(200, fixture_usage())
    _one(root, client, 1000.0)

    client.queue(NetworkError("could not reach api.anthropic.com: TimeoutError", "vibe list"))
    within = _one(root, client, 1000.0 + USAGE_FRESH_S)
    assert within.summary is not None
    assert within.message is not None
    assert "run:" not in within.message

    client.queue(NetworkError("could not reach api.anthropic.com: TimeoutError", "vibe list"))
    days_later = _one(root, client, 1000.0 + 8 * 86_400.0)
    assert days_later.state is AccountState.ERROR
    assert days_later.summary is None


def test_a_clock_stepped_back_does_not_freeze_the_cache(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"
    seed_account(root, "one")
    client = FakeHttpClient()
    client.queue_json(200, fixture_usage())
    _one(root, client, 10_000.0)

    client.queue_json(200, fixture_usage())
    view = _one(root, client, 10_000.0 - 3600.0)

    assert len(client.requests) == 2
    assert view.updated_at == 10_000.0 - 3600.0


def test_a_plan_less_account_asks_for_its_plan_only_when_it_fetches(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"
    seed_account(root, "one", credential=make_credential(subscription_type=None))
    client = FakeHttpClient()
    client.queue(HTTPError(429, b""))  # profile: swallowed, plan stays unknown
    client.queue_json(200, fixture_usage())
    _one(root, client, 1000.0)

    for later in (1001.0, 1050.0, 1100.0):
        _one(root, client, later)

    assert [request.url for request in client.requests] == [PROFILE_URL, USAGE_URL]


def test_renaming_an_account_keeps_its_backoff(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"
    seed_account(root, "old")
    client = FakeHttpClient()
    client.queue(HTTPError(429, b"", retry_after_s=900.0))
    _one(root, client, 1000.0)

    store.rename_account(root, "old", "new")
    (view,) = envelope.collect(root, client=client, now_s=1001.0)

    assert view.alias == "new"
    assert len(client.requests) == 1
    assert not store.usage_path(root, "old").exists()


def test_a_429_keeps_a_newer_answer_another_process_wrote(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"
    seed_account(root, "one")
    other = FakeHttpClient()
    other.queue_json(200, fixture_usage())
    _one(root, other, 1000.0)

    class LandsNewerFirst(FakeHttpClient):
        def request(
            self,
            method: str,
            url: str,
            *,
            headers: Mapping[str, str],
            body: bytes | None = None,
            timeout_s: float = 10.0,
        ) -> HttpResponse:
            # While this request is in flight, another process fetches.
            third = FakeHttpClient()
            third.queue_json(200, fixture_usage())
            _one(root, third, 1000.0 + USAGE_FRESH_S)
            return super().request(method, url, headers=headers, body=body, timeout_s=timeout_s)

    slow = LandsNewerFirst()
    slow.queue(HTTPError(429, b""))
    # Read the 1000.0 answer as stale, then 429 -- after the other process wrote its own.
    view = _one(root, slow, 1000.0 + USAGE_FRESH_S)

    assert view.updated_at == 1000.0 + USAGE_FRESH_S
    cache = store.read_usage_cache(root, "one")
    assert cache is not None
    assert cache.fetched_at == 1000.0 + USAGE_FRESH_S


def test_retry_after_is_read_from_the_response_headers() -> None:
    assert _retry_after_s("120") == 120.0
    assert _retry_after_s("Wed, 21 Oct 2026 07:28:00 GMT") is None
    assert _retry_after_s(None) is None
