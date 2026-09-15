"""Credential and identity values, and the two serialisations they have on disk.

The credential uses Claude Code's own key names, so the account file and the
Keychain blob share one shape and there is no translation layer.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from vibemaxxing.errors import StoreError
from vibemaxxing.redact import Secret

EXPIRY_BUFFER_MS: Final = 5 * 60 * 1000
OAUTH_MEMBER: Final = "claudeAiOauth"

_RECOVERY: Final = "vibe add"


@dataclass(frozen=True)
class Credential:
    access_token: Secret
    refresh_token: Secret
    expires_at_ms: int
    refresh_token_expires_at_ms: int | None
    scopes: tuple[str, ...]
    subscription_type: str | None
    rate_limit_tier: str | None


@dataclass(frozen=True)
class Identity:
    email: str | None
    account_uuid: str | None
    organization_name: str | None
    organization_uuid: str | None
    seat_tier: str | None
    billing_type: str | None
    display_name: str | None


EMPTY_IDENTITY: Final = Identity(None, None, None, None, None, None, None)


def _required_str(raw: Mapping[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise StoreError(f"credential field {key!r} is missing or not a string", _RECOVERY)
    return value


def _required_int(raw: Mapping[str, object], key: str) -> int:
    value = raw.get(key)
    # bool is an int subclass and would silently become 0/1.
    if not isinstance(value, int) or isinstance(value, bool):
        raise StoreError(f"credential field {key!r} is missing or not a number", _RECOVERY)
    return value


def _optional_int(raw: Mapping[str, object], key: str) -> int | None:
    value = raw.get(key)
    if value is None:
        return None
    return _required_int(raw, key)


def _optional_str(raw: Mapping[str, object], key: str) -> str | None:
    value = raw.get(key)
    return value if isinstance(value, str) else None


def parse_credential(raw: Mapping[str, object]) -> Credential:
    scopes = raw.get("scopes")
    return Credential(
        access_token=Secret(_required_str(raw, "accessToken")),
        refresh_token=Secret(_required_str(raw, "refreshToken")),
        expires_at_ms=_required_int(raw, "expiresAt"),
        refresh_token_expires_at_ms=_optional_int(raw, "refreshTokenExpiresAt"),
        scopes=tuple(s for s in scopes if isinstance(s, str)) if isinstance(scopes, list) else (),
        subscription_type=_optional_str(raw, "subscriptionType"),
        rate_limit_tier=_optional_str(raw, "rateLimitTier"),
    )


def parse_blob_members(raw: str) -> dict[str, object]:
    """The whole blob, siblings included. ``switch`` needs them to rebuild it."""
    parsed: object
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise StoreError("the stored credential blob is not JSON", _RECOVERY) from exc
    if not isinstance(parsed, dict):
        raise StoreError("the stored credential blob is not a JSON object", _RECOVERY)
    return {str(key): value for key, value in parsed.items()}


def parse_blob(raw: str) -> Credential:
    member = parse_blob_members(raw).get(OAUTH_MEMBER)
    if not isinstance(member, dict):
        raise StoreError(
            f"the stored credential blob has no {OAUTH_MEMBER!r} object",
            _RECOVERY,
        )
    return parse_credential({str(key): value for key, value in member.items()})


def credential_to_disk(c: Credential) -> dict[str, object]:
    return {
        "accessToken": c.access_token.reveal(),
        "refreshToken": c.refresh_token.reveal(),
        "expiresAt": c.expires_at_ms,
        "refreshTokenExpiresAt": c.refresh_token_expires_at_ms,
        "scopes": list(c.scopes),
        "subscriptionType": c.subscription_type,
        "rateLimitTier": c.rate_limit_tier,
    }


def credential_to_blob(c: Credential, base: Mapping[str, object] | None = None) -> str:
    # Replace only the claudeAiOauth member: the live blob has siblings
    # (mcpOAuth was measured on this machine) and writing a bare
    # {"claudeAiOauth": ...} would delete the user's MCP logins on the first switch.
    blob: dict[str, object] = dict(base) if base is not None else {}
    blob[OAUTH_MEMBER] = credential_to_disk(c)
    return json.dumps(blob)


def is_expired(c: Credential, *, now_ms: int, buffer_ms: int = EXPIRY_BUFFER_MS) -> bool:
    return now_ms + buffer_ms >= c.expires_at_ms


def login_lapsed(c: Credential, *, now_ms: int) -> bool:
    return c.refresh_token_expires_at_ms is not None and now_ms >= c.refresh_token_expires_at_ms


def read_claude_identity(path: Path) -> Identity:
    # Identity is decoration: it names an account in the UI and costs no request.
    # A missing or broken ~/.claude.json belongs to Claude Code, not to us, so it
    # degrades to an unnamed account rather than failing the command.
    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return EMPTY_IDENTITY
    if not isinstance(parsed, dict):
        return EMPTY_IDENTITY
    account = parsed.get("oauthAccount")
    if not isinstance(account, dict):
        return EMPTY_IDENTITY
    raw = {str(key): value for key, value in account.items()}
    return Identity(
        email=_optional_str(raw, "emailAddress"),
        account_uuid=_optional_str(raw, "accountUuid"),
        organization_name=_optional_str(raw, "organizationName"),
        organization_uuid=_optional_str(raw, "organizationUuid"),
        seat_tier=_optional_str(raw, "seatTier"),
        billing_type=_optional_str(raw, "billingType"),
        display_name=_optional_str(raw, "displayName"),
    )
