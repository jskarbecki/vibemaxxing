"""The usage endpoint client and the summary every dashboard renders.

``summarize`` reads ``limits`` and ``seven_day_breakdown`` and nothing else. The
live response carried 22 top-level keys where the fixture has 9, so a summary
built from the sibling ``five_hour`` / ``seven_day`` objects would drift the day
the server grows another one. ``kind`` is open-ended for the same reason: an
unrecognised one is labelled generically rather than rejected.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from vibemaxxing.httpclient import HttpClient
from vibemaxxing.redact import Secret

USAGE_URL: Final = "https://api.anthropic.com/api/oauth/usage"
PROFILE_URL: Final = "https://api.anthropic.com/api/oauth/profile"
BETA_HEADER: Final = "oauth-2025-04-20"

_LABELS: Final = {"session": "Session", "weekly_all": "Weekly · all models"}


def _get(client: HttpClient, url: str, token: Secret, timeout_s: float) -> dict[str, object]:
    response = client.request(
        "GET",
        url,
        # One of the five permitted reveal() sites (contract s5). The value goes
        # into the header and nowhere else — not into the URL, not into a log.
        headers={
            "Authorization": f"Bearer {token.reveal()}",
            "anthropic-beta": BETA_HEADER,
        },
        timeout_s=timeout_s,
    )
    return response.json()


def fetch_usage(client: HttpClient, token: Secret, *, timeout_s: float = 10.0) -> dict[str, object]:
    return _get(client, USAGE_URL, token, timeout_s)


def fetch_plan(
    client: HttpClient, token: Secret, *, timeout_s: float = 10.0
) -> tuple[str | None, str | None]:
    """The account's subscription type and rate limit tier, from the profile endpoint.

    Neither value is anywhere else: the usage response names no plan (measured —
    22 top-level keys, none of them a plan), and the token endpoint answers a
    `vibe add` login without one either, so an account we logged in ourselves has
    nothing to show until this runs. ``organization_type`` reads "claude_max" /
    "claude_pro"; the prefix comes off so the stored value matches what Claude
    Code writes into the credential blob, which is the same blob a switch ports.
    """
    payload = _get(client, PROFILE_URL, token, timeout_s)
    organization: object = payload.get("organization")
    if not isinstance(organization, Mapping):
        return None, None
    kind = _text(organization.get("organization_type"))
    tier = _text(organization.get("rate_limit_tier"))
    return (kind.removeprefix("claude_") if kind else None), tier


@dataclass(frozen=True)
class Row:
    kind: str
    label: str
    percent: int | None
    severity: str | None
    resets_at: str | None


@dataclass(frozen=True)
class BreakdownRow:
    label: str
    percent: int | None


@dataclass(frozen=True)
class Summary:
    rows: tuple[Row, ...]
    breakdown: tuple[BreakdownRow, ...]


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _percent(value: object) -> int | None:
    # bool is an int; a JSON `true` here is a shape error, not 1 %.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(value)


def _scoped_name(entry: Mapping[str, object]) -> str | None:
    scope: object = entry.get("scope")
    if not isinstance(scope, Mapping):
        return None
    model: object = scope.get("model")
    if not isinstance(model, Mapping):
        return None
    return _text(model.get("display_name"))


def _label(entry: Mapping[str, object], kind: str) -> str:
    if kind == "weekly_scoped":
        name = _scoped_name(entry)
        if name is not None:
            return f"Weekly · {name}"
    return _LABELS.get(kind) or kind.replace("_", " ").capitalize()


def _rows(payload: Mapping[str, object]) -> tuple[Row, ...]:
    limits: object = payload.get("limits")
    if not isinstance(limits, Sequence) or isinstance(limits, (str, bytes)):
        return ()
    rows: list[Row] = []
    for item in limits:
        entry: object = item
        if not isinstance(entry, Mapping):
            continue
        kind = _text(entry.get("kind"))
        if kind is None:
            continue
        rows.append(
            Row(
                kind=kind,
                label=_label(entry, kind),
                percent=_percent(entry.get("percent")),
                severity=_text(entry.get("severity")),
                resets_at=_text(entry.get("resets_at")),
            )
        )
    return tuple(rows)


def _breakdown(payload: Mapping[str, object]) -> tuple[BreakdownRow, ...]:
    section: object = payload.get("seven_day_breakdown")
    if not isinstance(section, Mapping):
        return ()
    raw: object = section.get("rows")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    rows: list[BreakdownRow] = []
    for item in raw:
        entry: object = item
        if not isinstance(entry, Mapping):
            continue
        label = _text(entry.get("display_name")) or _text(entry.get("key"))
        if label is None:
            continue
        rows.append(BreakdownRow(label=label, percent=_percent(entry.get("percent"))))
    return tuple(rows)


def summarize(payload: Mapping[str, object]) -> Summary:
    return Summary(rows=_rows(payload), breakdown=_breakdown(payload))
