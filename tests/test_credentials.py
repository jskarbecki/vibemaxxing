from __future__ import annotations

import json
from pathlib import Path

import pytest

from vibemaxxing.credentials import (
    EMPTY_IDENTITY,
    EXPIRY_BUFFER_MS,
    credential_to_blob,
    credential_to_disk,
    is_expired,
    login_lapsed,
    parse_blob,
    parse_credential,
    read_claude_identity,
)
from vibemaxxing.errors import StoreError

LIVE_BLOB = json.dumps(
    {
        "claudeAiOauth": {
            "accessToken": "sk-ant-oat01-access-aaaaaaaa",
            "refreshToken": "sk-ant-ort01-refresh-bbbbbbbb",
            "expiresAt": 1757930000000,
            "refreshTokenExpiresAt": 1760522000000,
            "scopes": ["user:profile", "user:inference"],
            "subscriptionType": "max",
            "rateLimitTier": "default_claude_max_20x",
            "amberLadder": {"a member the server grew without warning": True},
        },
        "mcpOAuth": {"linear": {"accessToken": "mcp-token-value"}},
    }
)


def test_blob_round_trip_keeps_siblings_and_ignores_unknown_members() -> None:
    credential = parse_blob(LIVE_BLOB)
    assert credential.access_token.reveal() == "sk-ant-oat01-access-aaaaaaaa"
    assert credential.refresh_token.reveal() == "sk-ant-ort01-refresh-bbbbbbbb"
    assert credential.expires_at_ms == 1757930000000
    assert credential.refresh_token_expires_at_ms == 1760522000000
    assert credential.scopes == ("user:profile", "user:inference")
    assert credential.subscription_type == "max"
    assert credential.rate_limit_tier == "default_claude_max_20x"

    written = json.loads(credential_to_blob(credential, json.loads(LIVE_BLOB)))
    assert written["mcpOAuth"] == {"linear": {"accessToken": "mcp-token-value"}}
    assert set(written["claudeAiOauth"]) == {
        "accessToken",
        "refreshToken",
        "expiresAt",
        "refreshTokenExpiresAt",
        "scopes",
        "subscriptionType",
        "rateLimitTier",
    }
    assert credential_to_disk(credential) == written["claudeAiOauth"]
    assert json.loads(credential_to_blob(credential)) == {"claudeAiOauth": written["claudeAiOauth"]}


def test_expiry_and_lapse_are_epoch_milliseconds() -> None:
    credential = parse_blob(LIVE_BLOB)
    assert not is_expired(credential, now_ms=1757930000000 - EXPIRY_BUFFER_MS - 1)
    assert is_expired(credential, now_ms=1757930000000 - EXPIRY_BUFFER_MS)
    assert not is_expired(credential, now_ms=1757930000000 - 1, buffer_ms=0)

    assert not login_lapsed(credential, now_ms=1760521999999)
    assert login_lapsed(credential, now_ms=1760522000000)


def test_bad_credential_shape_raises_store_error_with_recovery() -> None:
    with pytest.raises(StoreError) as bad_token:
        parse_credential({"accessToken": 7, "refreshToken": "x", "expiresAt": 1})
    assert bad_token.value.recovery is not None
    assert "accessToken" in bad_token.value.message

    with pytest.raises(StoreError):
        parse_credential({"accessToken": "a", "refreshToken": "b"})
    with pytest.raises(StoreError):
        parse_blob("not json at all")
    with pytest.raises(StoreError):
        parse_blob(json.dumps({"mcpOAuth": {}}))


def test_identity_comes_from_claude_json_and_is_empty_when_absent(tmp_home: Path) -> None:
    assert read_claude_identity(tmp_home / ".claude.json") == EMPTY_IDENTITY

    path = tmp_home / "config.json"
    path.write_text(
        json.dumps(
            {
                "oauthAccount": {
                    "emailAddress": "jan@intra-ai.de",
                    "accountUuid": "acct-uuid",
                    "organizationName": "Intra AI",
                    "organizationUuid": "org-uuid",
                    "seatTier": "enterprise",
                    "billingType": "seat",
                    "displayName": "Jan",
                    "organizationRole": "admin",
                },
                "somethingElse": 1,
            }
        )
    )
    identity = read_claude_identity(path)
    assert identity.email == "jan@intra-ai.de"
    assert identity.account_uuid == "acct-uuid"
    assert identity.organization_name == "Intra AI"
    assert identity.organization_uuid == "org-uuid"
    assert identity.seat_tier == "enterprise"
    assert identity.billing_type == "seat"
    assert identity.display_name == "Jan"

    broken = tmp_home / "broken.json"
    broken.write_text("{not json")
    assert read_claude_identity(broken) == EMPTY_IDENTITY
