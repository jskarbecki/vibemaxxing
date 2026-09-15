"""The OAuth flow: PKCE authorize URL, paste parsing, exchange, and the consume gate.

A refresh rotates the refresh token — the server issues a successor and kills the
predecessor. The successor is therefore written to the stash before the account
file, so a crash anywhere leaves a recoverable token on disk instead of an account
holding one the server already revoked.

No lock is ever held across the POST. The in-flight exclusion is the claim file
with its lease, taken and released around the request, never during it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from vibemaxxing import store
from vibemaxxing.credentials import Credential
from vibemaxxing.errors import NeedsLoginError, NetworkError, PasteFormatError, StateMismatchError
from vibemaxxing.httpclient import HttpClient, HTTPError
from vibemaxxing.redact import Secret

AUTHORIZE_URL: Final = "https://claude.ai/oauth/authorize"
TOKEN_URL: Final = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID: Final = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
REDIRECT_URI: Final = "https://platform.claude.com/oauth/code/callback"
SCOPES: Final = (
    "org:create_api_key",
    "user:profile",
    "user:inference",
    "user:sessions:claude_code",
    "user:mcp_servers",
    "user:file_upload",
)

PERMANENT_ERRORS: Final = ("invalid_grant", "invalid_client")
PERMANENT_STATUSES: Final = (400, 401, 403)

_RECOVERY: Final = "vibe add"


@dataclass(frozen=True)
class RefreshOutcome:
    credential: Credential | None
    error: str | None
    stashed: bool = False


def build_authorize_url(verifier: str, state: str) -> str:
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    query = urllib.parse.urlencode(
        {
            "code": "true",
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": " ".join(SCOPES),
            "code_challenge": challenge.decode().rstrip("="),
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def parse_pasted_code(paste: str, expected_state: str) -> tuple[str, str]:
    code, separator, state = paste.strip().partition("#")
    if not separator:
        raise PasteFormatError(
            'the pasted value has no "#": copy the whole code from the callback page',
            _RECOVERY,
        )
    if state != expected_state:
        raise StateMismatchError(
            "the pasted code belongs to a different login attempt",
            _RECOVERY,
        )
    return code, state


def classify_refresh_error(exc: HTTPError) -> str:
    if exc.status not in PERMANENT_STATUSES:
        return "transient"
    parsed: object
    try:
        parsed = json.loads(exc.body)
    except ValueError:
        return "transient"
    if not isinstance(parsed, dict):
        return "transient"
    # The top-level member only. The marker appears inside other envelopes' detail
    # text, and a false permanent verdict quarantines a live account.
    error = parsed.get("error")
    if isinstance(error, str) and error in PERMANENT_ERRORS:
        return error
    return "transient"


def _credential_from_token(
    payload: Mapping[str, object], *, now_ms: int, previous: Credential | None
) -> Credential:
    access = payload.get("access_token")
    expires_in = payload.get("expires_in")
    if not isinstance(access, str) or not access:
        raise NetworkError("the token endpoint sent no access_token", _RECOVERY)
    if not isinstance(expires_in, (int, float)) or isinstance(expires_in, bool):
        raise NetworkError("the token endpoint sent no usable expires_in", _RECOVERY)

    rotated = payload.get("refresh_token")
    if isinstance(rotated, str) and rotated:
        refresh_token = Secret(rotated)
    elif previous is not None:
        refresh_token = previous.refresh_token
    else:
        raise NetworkError("the token endpoint sent no refresh_token", _RECOVERY)

    scope = payload.get("scope")
    if isinstance(scope, str) and scope:
        scopes = tuple(scope.split())
    else:
        scopes = previous.scopes if previous is not None else ()

    return Credential(
        access_token=Secret(access),
        refresh_token=refresh_token,
        expires_at_ms=now_ms + int(expires_in * 1000),
        # The contract does not document the token response beyond the request
        # body, so these three carry over from the predecessor rather than being
        # read from invented members.
        refresh_token_expires_at_ms=previous.refresh_token_expires_at_ms if previous else None,
        scopes=scopes,
        subscription_type=previous.subscription_type if previous else None,
        rate_limit_tier=previous.rate_limit_tier if previous else None,
    )


def _post_token(client: HttpClient, body: Mapping[str, object]) -> Mapping[str, object]:
    response = client.request(
        "POST",
        TOKEN_URL,
        headers={"Content-Type": "application/json"},
        body=json.dumps(body).encode(),
    )
    return response.json()


def refresh(
    root: Path,
    alias: str,
    credential: Credential,
    client: HttpClient,
    *,
    now_ms: int,
) -> RefreshOutcome:
    # A stash means the last run was interrupted after the server rotated the
    # token. Posting the predecessor again would just earn an invalid_grant.
    successor = store.read_stash(root, alias)

    if successor is None:
        if not credential.refresh_token:
            return RefreshOutcome(None, "no_refresh_token")
        if not store.claim_refresh(root, alias, now_s=now_ms / 1000):
            return RefreshOutcome(None, "busy")
        try:
            payload = _post_token(
                client,
                {
                    "grant_type": "refresh_token",
                    "refresh_token": credential.refresh_token.reveal(),
                    "client_id": CLIENT_ID,
                },
            )
            successor = _credential_from_token(payload, now_ms=now_ms, previous=credential)
        except HTTPError as exc:
            store.release_refresh(root, alias)
            return RefreshOutcome(None, classify_refresh_error(exc))
        except NetworkError:
            store.release_refresh(root, alias)
            return RefreshOutcome(None, "transient")
        store.write_stash(root, alias, successor)

    # finally, not a release per branch: read_account raises StoreError or
    # NotFoundError on a corrupt account file, and letting that escape without
    # releasing would wedge the alias at "busy" for the whole claim lease.
    try:
        try:
            account = store.read_account(root, alias)
            store.write_account(root, replace(account, credential=successor))
        except OSError:
            # The successor is durable but the account file does not hold it
            # yet, so the caller may use the credential and must not claim the
            # account was updated. The stash stays until the next run.
            return RefreshOutcome(successor, "transient", stashed=True)
        store.delete_stash(root, alias)
        return RefreshOutcome(successor, None)
    finally:
        store.release_refresh(root, alias)


def exchange_code(client: HttpClient, *, code: str, verifier: str, state: str) -> Credential:
    try:
        payload = _post_token(
            client,
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "state": state,
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
            },
        )
    except HTTPError as exc:
        raise NeedsLoginError(
            f"the login could not be completed (HTTP {exc.status})",
            _RECOVERY,
        ) from None
    return _credential_from_token(payload, now_ms=int(time.time() * 1000), previous=None)
