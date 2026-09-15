from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from tests.fakes import FakeKeychain
from vibemaxxing import store
from vibemaxxing.credentials import EMPTY_IDENTITY, Credential
from vibemaxxing.errors import NotFoundError, StoreError, UsageError
from vibemaxxing.models import AccountState
from vibemaxxing.redact import Secret
from vibemaxxing.store import (
    CLAIM_LEASE_S,
    Account,
    account_path,
    claim_refresh,
    delete_account,
    history_path,
    list_aliases,
    read_account,
    read_active,
    read_stash,
    release_refresh,
    rename_account,
    stash_path,
    store_root,
    switch,
    valid_alias,
    write_account,
    write_active,
    write_stash,
)


def make_credential(
    access: str = "access-token-aaaaaaaa",
    refresh: str = "refresh-token-bbbbbbbb",
) -> Credential:
    return Credential(
        access_token=Secret(access),
        refresh_token=Secret(refresh),
        expires_at_ms=1757930000000,
        refresh_token_expires_at_ms=1760522000000,
        scopes=("user:profile",),
        subscription_type="max",
        rate_limit_tier="default_claude_max_20x",
    )


def make_account(alias: str, credential: Credential | None = None) -> Account:
    return Account(
        alias=alias,
        credential=credential if credential is not None else make_credential(),
        identity=EMPTY_IDENTITY,
        state=AccountState.OK,
        message=None,
        added_at=1757930000.0,
    )


def test_store_paths_are_owner_only(tmp_home: Path) -> None:
    root = store_root()
    assert root == tmp_home / ".vibemaxxing"
    assert history_path(root) == root / "history.db"

    write_account(root, make_account("work"))
    write_stash(root, "work", make_credential(access="successor-token-cccccccc"))
    write_active(root, "work")
    assert claim_refresh(root, "work", now_s=1000.0)

    files = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        assert stat.S_IMODE(os.stat(dirpath).st_mode) == 0o700, dirpath
        for name in filenames:
            path = os.path.join(dirpath, name)
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600, path
            files += 1
    assert files == 4

    stashed = read_stash(root, "work")
    assert stashed is not None
    assert stashed.access_token.reveal() == "successor-token-cccccccc"


def test_switch_resyncs_outgoing_then_writes_incoming(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = store_root()
    write_account(
        root,
        make_account("a", make_credential("a-stale-access-tok", "a-stale-refresh-tok")),
    )
    write_account(
        root, make_account("b", make_credential("b-access-token-xx", "b-refresh-token-xx"))
    )
    write_active(root, "a")

    port = FakeKeychain(
        blob=json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "a-rotated-behind-our-back",
                    "refreshToken": "a-rotated-refresh-token",
                    "expiresAt": 1757930000000,
                    "refreshTokenExpiresAt": 1760522000000,
                    "scopes": ["user:profile"],
                    "subscriptionType": "max",
                    "rateLimitTier": "default_claude_max_20x",
                },
                "mcpOAuth": {"linear": {"accessToken": "mcp-token-value"}},
            }
        )
    )

    real_write_account = store.write_account

    def logged(root_: Path, account: Account) -> None:
        port.log.append(f"write_account:{account.alias}")
        real_write_account(root_, account)

    monkeypatch.setattr(store, "write_account", logged)

    switch(root, "b", port)

    assert port.log == ["keychain.read", "write_account:a", "keychain.write"]

    assert port.blob is not None
    written = json.loads(port.blob)
    assert written["claudeAiOauth"]["accessToken"] == "b-access-token-xx"
    assert written["mcpOAuth"] == {"linear": {"accessToken": "mcp-token-value"}}

    assert read_account(root, "a").credential.access_token.reveal() == "a-rotated-behind-our-back"
    assert read_active(root) == "b"


def test_switch_refuses_an_unknown_alias_before_touching_the_port(tmp_home: Path) -> None:
    port = FakeKeychain(blob='{"claudeAiOauth": {}}')
    with pytest.raises(NotFoundError):
        switch(store_root(), "ghost", port)
    assert port.log == []


def test_account_round_trip_rename_and_delete(tmp_home: Path) -> None:
    root = store_root()
    account = make_account("work")
    write_account(root, account)
    assert read_account(root, "work") == account
    assert list_aliases(root) == ["work"]

    write_active(root, "work")
    write_stash(root, "work", make_credential(access="successor-token-cccccccc"))
    rename_account(root, "work", "home")

    assert list_aliases(root) == ["home"]
    assert read_account(root, "home").alias == "home"
    assert read_active(root) == "home"
    stashed = read_stash(root, "home")
    assert stashed is not None
    assert stashed.access_token.reveal() == "successor-token-cccccccc"
    assert read_stash(root, "work") is None
    with pytest.raises(NotFoundError):
        read_account(root, "work")

    write_account(root, make_account("other"))
    with pytest.raises(UsageError):
        rename_account(root, "other", "home")

    delete_account(root, "home")
    delete_account(root, "other")
    assert list_aliases(root) == []
    assert read_active(root) is None
    with pytest.raises(NotFoundError):
        rename_account(root, "home", "elsewhere")


def test_unknown_schema_is_never_migrated(tmp_home: Path) -> None:
    root = store_root()
    write_account(root, make_account("work"))
    path = account_path(root, "work")

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["schema"] = 99
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(StoreError) as excinfo:
        read_account(root, "work")
    assert str(path) in excinfo.value.message
    assert excinfo.value.recovery == "vibe add work"

    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(StoreError):
        read_account(root, "work")


def test_claim_refresh_is_exclusive_until_the_lease_lapses(tmp_home: Path) -> None:
    root = store_root()
    assert claim_refresh(root, "work", now_s=1000.0) is True
    assert claim_refresh(root, "work", now_s=1000.0) is False
    assert claim_refresh(root, "work", now_s=1000.0 + CLAIM_LEASE_S - 0.1) is False
    # A claimer that crashed before releasing must not wedge the alias forever.
    assert claim_refresh(root, "work", now_s=1000.0 + CLAIM_LEASE_S) is True

    release_refresh(root, "work")
    assert claim_refresh(root, "work", now_s=2000.0) is True
    release_refresh(root, "work")
    release_refresh(root, "work")


def test_invalid_alias_never_escapes_the_store(tmp_home: Path) -> None:
    root = store_root()
    assert valid_alias("work")
    assert valid_alias("a.b-c_1")
    assert valid_alias("a" * 64)
    for bad in ("", ".hidden", "-lead", "../etc/passwd", "a/b", "has space", "a" * 65):
        assert not valid_alias(bad)

    for bad in ("../escape", "a/b", ""):
        with pytest.raises(UsageError):
            account_path(root, bad)
        with pytest.raises(UsageError):
            stash_path(root, bad)
