"""Test doubles shared by every slice. One fake per port, so the slices agree."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field

from vibemaxxing.httpclient import HttpResponse


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
