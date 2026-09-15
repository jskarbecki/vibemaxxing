"""Test doubles shared by every slice. One fake per port, so the slices agree."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from vibemaxxing import store
from vibemaxxing.credentials import EMPTY_IDENTITY, Credential, Identity
from vibemaxxing.httpclient import HttpResponse
from vibemaxxing.models import AccountState
from vibemaxxing.redact import Secret
from vibemaxxing.store import Account


@dataclass
class RecordedRequest:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None

    def json_body(self) -> dict[str, object]:
        return json.loads(self.body or b"{}")


@dataclass
class FakeHttpClient:
    """Queued responses, recorded requests. No socket is ever opened.

    Queue an ``Exception`` instance to make the next call raise it — that is how
    an HTTPError path is exercised.
    """

    responses: list[HttpResponse | Exception] = field(default_factory=list)
    requests: list[RecordedRequest] = field(default_factory=list)

    def queue_json(self, status: int, payload: object) -> None:
        self.responses.append(HttpResponse(status, json.dumps(payload).encode()))

    def queue(self, item: HttpResponse | Exception) -> None:
        self.responses.append(item)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
        timeout_s: float = 10.0,
    ) -> HttpResponse:
        self.requests.append(RecordedRequest(method, url, dict(headers), body))
        if not self.responses:
            raise AssertionError(f"FakeHttpClient got an unexpected request: {method} {url}")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


@dataclass
class FakeKeychain:
    """In-memory KeychainPort. ``log`` records read/write order for AC10."""

    blob: str | None = None
    log: list[str] = field(default_factory=list)

    def read(self) -> str | None:
        self.log.append("keychain.read")
        return self.blob

    def write(self, blob: str) -> None:
        self.log.append("keychain.write")
        self.blob = blob


def make_credential(
    *,
    access: str = "access-token-value",
    refresh: str = "refresh-token-value",
    expires_at_ms: int = 4_102_444_800_000,
    refresh_expires_at_ms: int | None = 4_102_444_800_000,
    subscription_type: str | None = "max",
) -> Credential:
    return Credential(
        access_token=Secret(access),
        refresh_token=Secret(refresh),
        expires_at_ms=expires_at_ms,
        refresh_token_expires_at_ms=refresh_expires_at_ms,
        scopes=("user:profile", "user:inference"),
        subscription_type=subscription_type,
        rate_limit_tier="default",
    )


def seed_account(
    root: Path,
    alias: str,
    *,
    credential: Credential | None = None,
    identity: Identity | None = None,
    active: bool = False,
    added_at: float = 1_757_930_000.0,
) -> Account:
    account = Account(
        alias=alias,
        credential=credential if credential is not None else make_credential(),
        identity=identity if identity is not None else EMPTY_IDENTITY,
        state=AccountState.OK,
        message=None,
        added_at=added_at,
    )
    store.write_account(root, account)
    if active:
        store.write_active(root, alias)
    return account


def fixture_usage() -> dict[str, object]:
    path = Path(__file__).with_name("fixture_usage.json")
    payload = json.loads(path.read_text())
    assert isinstance(payload, dict)
    return payload


# Contract section 10's account entry. One definition, so two surfaces' tests
# cannot disagree about what the envelope promises.
ENVELOPE_ACCOUNT_KEYS = frozenset(
    {
        "alias",
        "active",
        "state",
        "message",
        "email",
        "display_name",
        "organization",
        "plan",
        "updated_at",
        "rows",
        "breakdown",
    }
)
