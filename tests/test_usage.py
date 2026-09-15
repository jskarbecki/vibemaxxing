from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.fakes import FakeHttpClient
from vibemaxxing.errors import NetworkError
from vibemaxxing.httpclient import HttpResponse
from vibemaxxing.redact import Secret
from vibemaxxing.usage import BETA_HEADER, USAGE_URL, fetch_usage, summarize

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
