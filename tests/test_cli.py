"""CLI surface: the --json envelope, exit codes, and that nothing prints a token."""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

import pytest

from tests.fakes import FakeHttpClient, FakeKeychain, fixture_usage, make_credential, seed_account
from vibemaxxing import cli, credentials, store
from vibemaxxing.credentials import Identity
from vibemaxxing.envelope import ENVELOPE_SCHEMA

NOW_S = 1_757_930_000.0
NOW_MS = int(NOW_S * 1000)
SENTINEL = "VMXSENTINEL0000000000"

ACCOUNT_KEYS = {
    "alias",
    "active",
    "state",
    "message",
    "email",
    "organization",
    "plan",
    "updated_at",
    "rows",
    "breakdown",
}


def context(tmp_home: Path, client: FakeHttpClient, **kw: object) -> cli.Context:
    return cli.Context(
        root=tmp_home / ".vibemaxxing",
        client=client,
        port=FakeKeychain(),
        now_s=NOW_S,
        prompt=lambda _: "",
        browser=lambda _: True,
        **kw,  # type: ignore[arg-type]
    )


def test_list_json_envelope_is_stable(tmp_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_home / ".vibemaxxing"
    seed_account(
        root,
        "healthy",
        credential=make_credential(access="live-access", refresh="live-refresh"),
        identity=Identity(
            email="jan@intra-ai.de",
            account_uuid="uuid-1",
            organization_name="Intra AI",
            organization_uuid="org-1",
            seat_tier="max",
            billing_type="stripe",
            display_name="Jan",
        ),
        active=True,
    )
    # A login whose refresh token itself lapsed: only a fresh /login fixes it.
    seed_account(
        root,
        "lapsed",
        credential=make_credential(refresh_expires_at_ms=NOW_MS - 1),
    )

    client = FakeHttpClient()
    client.queue_json(200, fixture_usage())
    assert cli.main(["list", "--json"], context=context(tmp_home, client)) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == ENVELOPE_SCHEMA
    assert set(payload) == {"schema", "generated_at", "accounts", "pool"}
    assert set(payload["pool"]) == {"accounts", "remaining_account_weeks", "dry_in_seconds"}
    assert payload["pool"]["accounts"] == 2

    entries = {entry["alias"]: entry for entry in payload["accounts"]}
    assert set(entries) == {"healthy", "lapsed"}
    for entry in entries.values():
        assert set(entry) == ACCOUNT_KEYS, "every documented key on every entry, whatever the state"

    healthy = entries["healthy"]
    assert healthy["state"] == "ok"
    assert healthy["active"] is True
    assert healthy["email"] == "jan@intra-ai.de"
    assert healthy["organization"] == "Intra AI"
    assert healthy["plan"] == "max"
    assert [row["percent"] for row in healthy["rows"]] == [0, 56, 66]
    assert [row["label"] for row in healthy["breakdown"]] == [
        "Claude Code",
        "Chats",
        "Cowork",
        "Other",
    ]

    lapsed = entries["lapsed"]
    assert lapsed["state"] == "needs_login"
    assert lapsed["active"] is False
    assert "vibe add" in lapsed["message"]
    assert lapsed["rows"] == []
    assert lapsed["breakdown"] == []
    assert lapsed["updated_at"] is None
    # An unusable account contributes nothing but is still counted.
    assert payload["pool"]["remaining_account_weeks"] == pytest.approx(0.44)


def test_unknown_alias_exits_four_and_names_the_recovery(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = context(tmp_home, FakeHttpClient())
    assert cli.main(["switch", "nope"], context=ctx) == 4
    assert "vibe" in capsys.readouterr().err


def test_json_failure_uses_the_error_envelope(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = context(tmp_home, FakeHttpClient())
    assert cli.main(["switch", "nope", "--json"], context=ctx) == 4
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == ENVELOPE_SCHEMA
    assert payload["error"]["code"] == "not_found"
    assert payload["error"]["recovery"]


def test_add_adopts_the_login_already_in_claude_code(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_home / ".vibemaxxing"
    blob = credentials.credential_to_blob(make_credential(), base={"mcpOAuth": {"server": "kept"}})
    ctx = context(tmp_home, FakeHttpClient())
    ctx.port = FakeKeychain(blob=blob)
    (tmp_home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "jan@intra-ai.de", "accountUuid": "u1"}})
    )

    assert cli.main(["add"], context=ctx) == 0
    assert store.list_aliases(root) == ["jan"]
    assert store.read_active(root) == "jan"
    assert store.read_account(root, "jan").identity.email == "jan@intra-ai.de"
    assert "jan" in capsys.readouterr().out
    # No network: adopting a login must work with the machine offline.
    assert ctx.client.requests == []


def test_alias_then_remove(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"
    seed_account(root, "one")
    ctx = context(tmp_home, FakeHttpClient())
    assert cli.main(["alias", "one", "two"], context=ctx) == 0
    assert store.list_aliases(root) == ["two"]
    assert cli.main(["remove", "two"], context=ctx) == 0
    assert store.list_aliases(root) == []


def test_run_pins_the_token_to_the_child_environment_only(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"
    seed_account(root, "work", credential=make_credential(access="child-token"))
    ctx = context(tmp_home, FakeHttpClient())
    before = ctx.port.blob

    probe = (
        "import os, sys; "
        "sys.exit(0 if os.environ.get('CLAUDE_CODE_OAUTH_TOKEN') == 'child-token' else 7)"
    )
    exit_code = cli.main(["run", "work", "--", sys.executable, "-c", probe], context=ctx)
    assert exit_code == 0, "the child did not see the alias's access token"
    assert ctx.port.blob == before, "run must not touch the global credential"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ, "the parent env must stay clean"


def test_no_phase_d_command_emits_a_token(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_home / ".vibemaxxing"
    seed_account(root, "work", credential=make_credential(access=SENTINEL, refresh=SENTINEL))
    forms = (
        SENTINEL,
        base64.b64encode(SENTINEL.encode()).decode(),
        json.dumps(SENTINEL)[1:-1],
    )

    invocations = (
        ["list"],
        ["list", "--json"],
        ["switch", "work"],
        ["switch", "work", "--json"],
        ["alias", "work", "work2"],
        ["alias", "work2", "work"],
        ["usage", "--once"],
        ["usage", "--once", "--json"],
        ["remove", "work"],
        ["remove", "work", "--json"],
    )
    for argv in invocations:
        client = FakeHttpClient()
        client.queue_json(200, fixture_usage())
        cli.main(argv, context=context(tmp_home, client))
        captured = capsys.readouterr()
        for form in forms:
            assert form not in captured.out, f"{argv} leaked to stdout"
            assert form not in captured.err, f"{argv} leaked to stderr"

    # The account file holds the credential at rest by frozen product decision
    # (plaintext JSON, 0600). Every other file in the store — the stash, the
    # claim, the active pointer, the raw bytes of the history database — must
    # be clean, and the account file must still be owner-only.
    scanned = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            assert path.stat().st_mode & 0o777 == 0o700, f"{path} is not 0700"
            continue
        assert path.stat().st_mode & 0o777 == 0o600, f"{path} is not 0600"
        if path.parent.name == "accounts":
            continue
        scanned += 1
        raw = path.read_bytes()
        for form in forms:
            assert form.encode() not in raw, f"{path} holds a token"
    assert scanned, "the scan matched no files, so it proved nothing"
