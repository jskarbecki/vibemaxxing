"""The one injectable HTTP client. Every outbound request in the package uses it."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Protocol

from vibemaxxing import __version__
from vibemaxxing.errors import NetworkError

ALLOWED_HOSTS: Final = frozenset({"claude.ai", "platform.claude.com", "api.anthropic.com"})
DEFAULT_TIMEOUT_S: Final = 10.0
# Cloudflare fronts all three hosts and answers urllib's default
# "Python-urllib/3.x" with 403 "error code: 1010" before the request ever
# reaches the API, so every login died at the token exchange. Any named
# agent gets through; this one says who we actually are.
USER_AGENT: Final = f"vibemaxxing/{__version__}"


class HTTPError(Exception):
    """A 4xx/5xx from an allowed host. ``str`` never includes the body."""

    def __init__(self, status: int, body: bytes) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes

    def json(self) -> dict[str, object]:
        parsed: object
        try:
            parsed = json.loads(self.body)
        except ValueError as exc:
            raise NetworkError(
                "the server sent a body that is not JSON",
                "vibe usage --once",
            ) from exc
        if not isinstance(parsed, dict):
            raise NetworkError(
                "the server sent JSON that is not an object",
                "vibe usage --once",
            )
        return {str(key): value for key, value in parsed.items()}


class HttpClient(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> HttpResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every 3xx.

    urllib's default opener follows a redirect and re-sends the Authorization
    header to whatever host the upstream names -- verified on loopback: a 302
    delivered `Bearer <token>` to a second server that was never compared
    against ALLOWED_HOSTS, and the caller still saw a clean 200. The allowlist
    is worthless if it only covers the first hop, and none of the three
    endpoints we speak to has any reason to redirect.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


_OPENER: Final = urllib.request.build_opener(_NoRedirect)


class UrllibClient:
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> HttpResponse:
        host = urllib.parse.urlsplit(url).hostname
        if host not in ALLOWED_HOSTS:
            raise NetworkError(
                f"refusing to contact {host!r}: vibemaxxing only talks to "
                + ", ".join(sorted(ALLOWED_HOSTS)),
                "vibe list",
            )
        sent = {"User-Agent": USER_AGENT, **headers}
        request = urllib.request.Request(url, data=body, headers=sent, method=method)
        try:
            with _OPENER.open(request, timeout=timeout_s) as response:
                return HttpResponse(int(response.status), response.read())
        except urllib.error.HTTPError as exc:
            # `from None`: the suppressed context holds the request object, whose
            # headers carry the bearer token.
            raise HTTPError(int(exc.code), exc.read()) from None
        except OSError as exc:
            raise NetworkError(
                f"could not reach {host}: {exc.__class__.__name__}",
                "vibe usage --once",
            ) from None
