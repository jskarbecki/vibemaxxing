from __future__ import annotations

import stat
import urllib.parse
from pathlib import Path

import pytest

from tests.fakes import FakeHttpClient
from vibemaxxing import store
from vibemaxxing.credentials import EMPTY_IDENTITY, Credential
from vibemaxxing.errors import NeedsLoginError, PasteFormatError, StateMismatchError
from vibemaxxing.httpclient import HTTPError
from vibemaxxing.models import AccountState
from vibemaxxing.oauth import (
    AUTHORIZE_URL,
    CLIENT_ID,
    REDIRECT_URI,
    TOKEN_URL,
    RefreshOutcome,
    build_authorize_url,
    classify_refresh_error,
    exchange_code,
    new_state,
    parse_pasted_code,
    refresh,
)
from vibemaxxing.redact import Secret
from vibemaxxing.store import (
    Account,
    claim_refresh,
    read_account,
    read_stash,
    stash_path,
    store_root,
    write_account,
)

NOW_MS = 1_000_000_000_000

# sha256("testverifier"), base64url, padding stripped.
TEST_CHALLENGE = "5ece6yyUyn-EB3AgU1LXI8kwWCmtccdYegzgbS6Shq4"


def make_credential(
    access: str = "access-predecessor-1",
    refresh_token: str = "refresh-predecessor-1",
) -> Credential:
    return Credential(
        access_token=Secret(access),
        refresh_token=Secret(refresh_token),
        expires_at_ms=NOW_MS,
        refresh_token_expires_at_ms=NOW_MS + 86_400_000,
        scopes=("user:profile",),
        subscription_type="max",
        rate_limit_tier="default_claude_max_20x",
    )


def make_account(alias: str, credential: Credential) -> Account:
    return Account(
        alias=alias,
        credential=credential,
        identity=EMPTY_IDENTITY,
        state=AccountState.OK,
        message=None,
        added_at=1757930000.0,
    )


def queue_successor(client: FakeHttpClient) -> None:
    client.queue_json(
        200,
        {
            "access_token": "access-successor-2",
            "refresh_token": "refresh-successor-2",
            "expires_in": 3600,
            "scope": "user:profile user:inference",
        },
    )


def test_authorize_url_matches_observed_login_flow() -> None:
    url = build_authorize_url("testverifier", "teststate")
    split = urllib.parse.urlsplit(url)
    assert f"{split.scheme}://{split.netloc}{split.path}" == AUTHORIZE_URL

    query = urllib.parse.parse_qs(split.query, strict_parsing=True)
    assert all(len(values) == 1 for values in query.values())
    assert {key: values[0] for key, values in query.items()} == {
        "code": "true",
        "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
        "response_type": "code",
        "redirect_uri": "https://platform.claude.com/oauth/code/callback",
        "scope": (
            "org:create_api_key user:profile user:inference "
            "user:sessions:claude_code user:mcp_servers user:file_upload"
        ),
        "code_challenge": TEST_CHALLENGE,
        "code_challenge_method": "S256",
        "state": "teststate",
    }


def test_state_is_long_enough_for_the_authorize_endpoint() -> None:
    # Observed 2026-09-15: a 22-char state (token_urlsafe(16)) renders the consent
    # page and then fails the Authorize click with "Invalid request format".
    # 43 chars is what Claude Code sends and what the endpoint accepts.
    assert len(new_state()) == 43


def test_pasted_code_splits_and_checks_state() -> None:
    assert parse_pasted_code("abc#def", "def") == ("abc", "def")

    with pytest.raises(PasteFormatError) as no_hash:
        parse_pasted_code("abc", "def")
    assert no_hash.value.recovery is not None

    with pytest.raises(StateMismatchError) as wrong_state:
        parse_pasted_code("abc#wrong", "def")
    assert wrong_state.value.recovery is not None


def test_refresh_error_read_from_top_level_error_member() -> None:
    assert classify_refresh_error(HTTPError(400, b'{"error":"invalid_grant"}')) == "invalid_grant"
    # A substring scan would call this permanent and quarantine a live account.
    assert (
        classify_refresh_error(HTTPError(400, b'{"detail":"...invalid_grant..."}')) == "transient"
    )
    assert classify_refresh_error(HTTPError(500, b'{"error":"invalid_grant"}')) == "transient"
    assert classify_refresh_error(HTTPError(403, b'{"error":"invalid_client"}')) == "invalid_client"
    assert classify_refresh_error(HTTPError(400, b"not json at all")) == "transient"
    assert classify_refresh_error(HTTPError(401, b'{"error":"slow_down"}')) == "transient"


def test_successor_is_stashed_when_account_write_fails(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = store_root()
    predecessor = make_credential()
    write_account(root, make_account("work", predecessor))

    client = FakeHttpClient()
    queue_successor(client)

    real_write_account = store.write_account
    failing = {"on": True}

    def maybe_boom(root_: Path, account: Account) -> None:
        if failing["on"]:
            raise OSError("no space left on device")
        real_write_account(root_, account)

    monkeypatch.setattr(store, "write_account", maybe_boom)

    first = refresh(root, "work", predecessor, client, now_ms=NOW_MS)
    assert first.stashed is True
    assert first.error == "transient"
    assert first.credential is not None
    assert first.credential.refresh_token.reveal() == "refresh-successor-2"
    assert first.credential.access_token.reveal() == "access-successor-2"
    assert first.credential.expires_at_ms == NOW_MS + 3_600_000

    path = stash_path(root, "work")
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    stashed = read_stash(root, "work")
    assert stashed is not None
    assert stashed.refresh_token.reveal() == "refresh-successor-2"
    assert read_account(root, "work").credential.refresh_token.reveal() == "refresh-predecessor-1"

    # The predecessor is spent. A second run must consume the stash, not POST it:
    # nothing is queued, so a POST would fail the test loudly.
    failing["on"] = False
    second = refresh(root, "work", predecessor, client, now_ms=NOW_MS)
    assert len(client.requests) == 1
    assert second == RefreshOutcome(first.credential, None)
    assert read_account(root, "work").credential.refresh_token.reveal() == "refresh-successor-2"
    assert not path.exists()


def test_refresh_swaps_the_credential_and_sends_the_contract_body(tmp_home: Path) -> None:
    root = store_root()
    predecessor = make_credential()
    write_account(root, make_account("work", predecessor))

    client = FakeHttpClient()
    queue_successor(client)
    outcome = refresh(root, "work", predecessor, client, now_ms=NOW_MS)

    assert outcome.error is None
    assert outcome.stashed is False
    assert outcome.credential is not None
    assert outcome.credential.scopes == ("user:profile", "user:inference")

    request = client.requests[0]
    assert request.method == "POST"
    assert request.url == TOKEN_URL
    assert request.json_body() == {
        "grant_type": "refresh_token",
        "refresh_token": "refresh-predecessor-1",
        "client_id": CLIENT_ID,
    }
    assert "Authorization" not in request.headers

    assert not stash_path(root, "work").exists()
    # The claim was released, so the next refresh is not locked out.
    assert claim_refresh(root, "work", now_s=NOW_MS / 1000) is True


def test_refresh_reports_a_dead_lineage_without_touching_the_account(tmp_home: Path) -> None:
    root = store_root()
    predecessor = make_credential()
    write_account(root, make_account("work", predecessor))

    client = FakeHttpClient()
    client.queue(HTTPError(400, b'{"error":"invalid_grant"}'))
    outcome = refresh(root, "work", predecessor, client, now_ms=NOW_MS)

    assert outcome == RefreshOutcome(None, "invalid_grant")
    assert read_account(root, "work").credential.refresh_token.reveal() == "refresh-predecessor-1"
    assert not stash_path(root, "work").exists()
    assert claim_refresh(root, "work", now_s=NOW_MS / 1000) is True


def test_refresh_reports_busy_while_a_claim_is_live(tmp_home: Path) -> None:
    root = store_root()
    predecessor = make_credential()
    write_account(root, make_account("work", predecessor))
    assert claim_refresh(root, "work", now_s=NOW_MS / 1000) is True

    client = FakeHttpClient()
    assert refresh(root, "work", predecessor, client, now_ms=NOW_MS) == RefreshOutcome(None, "busy")
    assert client.requests == []


def test_exchange_code_posts_the_contract_body() -> None:
    # This grant has never been executed against the live server. The fake pins
    # the request shape the contract specifies and nothing else depends on it.
    client = FakeHttpClient()
    client.queue_json(
        200,
        {
            "access_token": "fresh-access-token",
            "refresh_token": "fresh-refresh-token",
            "expires_in": 3600,
            "scope": "user:profile",
            "account": {"uuid": "acct-uuid", "email_address": "who@example.com"},
            "organization": {"uuid": "org-uuid", "name": "Example Org"},
        },
    )
    credential, identity = exchange_code(
        client, code="the-code", verifier="the-verifier", state="the-state"
    )
    assert credential.access_token.reveal() == "fresh-access-token"
    assert credential.scopes == ("user:profile",)
    # The token response optionally names the account, so a fresh login is not
    # nameless until Claude Code next rewrites ~/.claude.json.
    assert identity.email == "who@example.com"
    assert identity.account_uuid == "acct-uuid"
    assert identity.organization_name == "Example Org"

    request = client.requests[0]
    assert request.method == "POST"
    assert request.url == TOKEN_URL
    assert request.json_body() == {
        "grant_type": "authorization_code",
        "code": "the-code",
        "code_verifier": "the-verifier",
        "state": "the-state",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
    }
    assert "Authorization" not in request.headers

    client.queue(HTTPError(400, b'{"error":"invalid_grant"}'))
    with pytest.raises(NeedsLoginError) as excinfo:
        exchange_code(client, code="bad", verifier="v", state="s")
    assert excinfo.value.recovery is not None
