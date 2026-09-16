"""One test per Phase F finding that was fixed. Named by finding id so a
regression points straight back at docs/AUDIT.md."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from tests.fakes import FakeHttpClient, FakeKeychain, make_credential, seed_account
from vibemaxxing import cli, credentials, httpclient, store
from vibemaxxing.credentials import Identity
from vibemaxxing.errors import UsageError
from vibemaxxing.httpclient import HTTPError, UrllibClient

PROBE = "PROBE-NOT-A-REAL-TOKEN-0000"
NOW_S = 1_757_930_000.0


def _ctx(tmp_home: Path, client: FakeHttpClient | None = None) -> cli.Context:
    return cli.Context(
        root=tmp_home / ".vibemaxxing",
        client=client if client is not None else FakeHttpClient(),
        port=FakeKeychain(),
        now_s=NOW_S,
        clock=lambda: NOW_S,
        prompt=lambda _: "",
        browser=lambda _: True,
    )


def _server(handler: type[BaseHTTPRequestHandler]) -> tuple[HTTPServer, int]:
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def test_redirect_never_carries_the_bearer_token_off_the_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """credential-leak:src/vibemaxxing/httpclient.py:redirect-bypasses-host-allowlist

    urllib's default opener follows a 3xx and re-sends Authorization to whatever
    host the upstream names. Verified before the fix: a second server that was
    never in ALLOWED_HOSTS received the bearer token and the caller saw a 200.
    """
    seen: dict[str, str | None] = {}

    class Collector(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen["auth"] = self.headers.get("Authorization")
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args: object) -> None: ...

    collector, cport = _server(Collector)

    class Redirector(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{cport}/exfil")
            self.end_headers()

        def log_message(self, *args: object) -> None: ...

    redirector, rport = _server(Redirector)
    monkeypatch.setattr(httpclient, "ALLOWED_HOSTS", frozenset({"127.0.0.1"}))
    try:
        with pytest.raises(HTTPError) as excinfo:
            UrllibClient().request(
                "GET",
                f"http://127.0.0.1:{rport}/api/oauth/usage",
                headers={"Authorization": f"Bearer {PROBE}"},
            )
        assert 300 <= excinfo.value.status < 400, "a 3xx must surface, not be followed"
    finally:
        for server in (collector, redirector):
            server.shutdown()
            server.server_close()
    assert seen.get("auth") is None, "the token crossed a redirect to an unlisted host"


def test_every_request_names_a_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cloudflare answers urllib's default "Python-urllib/3.x" with 403
    "error code: 1010" on all three allowed hosts, which killed the token
    exchange at the end of every login. Verified 2026-09-15 against
    platform.claude.com: no User-Agent -> 403, "vibemaxxing/0.1.0" -> a real
    invalid_grant 400."""
    seen: dict[str, str | None] = {}

    class Echo(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen["ua"] = self.headers.get("User-Agent")
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args: object) -> None: ...

    server, port = _server(Echo)
    monkeypatch.setattr(httpclient, "ALLOWED_HOSTS", frozenset({"127.0.0.1"}))
    try:
        UrllibClient().request("GET", f"http://127.0.0.1:{port}/api", headers={})
    finally:
        server.shutdown()
        server.server_close()
    assert seen.get("ua") == httpclient.USER_AGENT
    assert "urllib" not in (seen.get("ua") or "")


def test_switch_never_attributes_the_keychain_blob_to_the_wrong_account(
    tmp_home: Path,
) -> None:
    """credential-leak:src/vibemaxxing/store.py:switch-resync-attributes-the-keychain-blob-to-the-active-alias

    If the user runs `claude /login` behind our back, the port holds a different
    account than `active` names. Copying it into that account's file destroys
    the displaced refresh token.
    """
    root = tmp_home / ".vibemaxxing"
    a = Identity("a@x.com", "uuid-A", None, None, None, None, None)
    b = Identity("b@x.com", "uuid-B", None, None, None, None, None)
    seed_account(root, "work", credential=make_credential(refresh="WORK-REFRESH"), identity=a)
    seed_account(
        root,
        "personal",
        credential=make_credential(refresh="PERSONAL-REFRESH"),
        identity=b,
        active=True,
    )
    # The Keychain holds work's blob while `active` still says personal.
    port = FakeKeychain(
        blob=credentials.credential_to_blob(make_credential(refresh="WORK-REFRESH"))
    )
    (tmp_home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "a@x.com", "accountUuid": "uuid-A"}})
    )

    store.switch(root, "work", port)

    survived = store.read_account(root, "personal").credential.refresh_token.reveal()
    assert survived == "PERSONAL-REFRESH", "work's token was written into personal's file"


def test_an_alias_differing_only_in_case_is_refused(tmp_home: Path) -> None:
    """cross-platform:src/vibemaxxing/store.py:alias-namespace-collides-on-case-insensitive-fs

    APFS is case-insensitive, so accounts/Jan.json and accounts/jan.json are one
    file and the second write silently destroys the first account.
    """
    root = tmp_home / ".vibemaxxing"
    seed_account(root, "jan", credential=make_credential(refresh="FIRST-REFRESH"))
    with pytest.raises(UsageError) as excinfo:
        store.read_account(root, "Jan")
    assert "lower-case" in excinfo.value.message
    assert store.read_account(root, "jan").credential.refresh_token.reveal() == "FIRST-REFRESH"


def test_global_json_flag_is_not_overwritten_by_the_subparser(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """spec-conformance:src/vibemaxxing/cli.py:global-json-flag-silently-ignored"""
    assert cli.main(["--json", "switch", "nope"], context=_ctx(tmp_home)) == 4
    json.loads(capsys.readouterr().out)  # raises if human text was emitted


def test_remove_of_an_unknown_alias_exits_four(tmp_home: Path) -> None:
    """spec-conformance:src/vibemaxxing/cli.py:remove-reports-success-for-an-unknown-alias"""
    assert cli.main(["remove", "ghost"], context=_ctx(tmp_home)) == 4


def test_run_keeps_a_separator_the_child_needs(tmp_home: Path) -> None:
    """spec-conformance:src/vibemaxxing/cli.py:run-strips-every-separator-from-the-child-argv"""
    import sys as _sys

    root = tmp_home / ".vibemaxxing"
    seed_account(root, "work", credential=make_credential(access="t"))
    probe = "import sys; sys.exit(0 if sys.argv[1:] == ['--', 'inner'] else 9)"
    argv = ["run", "work", "--", _sys.executable, "-c", probe, "--", "inner"]
    assert cli.main(argv, context=_ctx(tmp_home)) == 0


def test_a_transient_refresh_is_not_reported_as_a_dead_login(tmp_home: Path) -> None:
    """spec-conformance:src/vibemaxxing/cli.py:run-reports-a-transient-refresh-as-needs-login"""
    from vibemaxxing.errors import NetworkError

    root = tmp_home / ".vibemaxxing"
    seed_account(root, "work", credential=make_credential(expires_at_ms=1))
    client = FakeHttpClient()
    client.queue(NetworkError("the network is down", "vibe list"))
    ctx = _ctx(tmp_home, client)
    assert cli.main(["run", "work", "--", "true"], context=ctx) == 6, (
        "exit 6 is network, 3 is login"
    )


def test_adopting_the_same_login_twice_does_not_duplicate_it(tmp_home: Path) -> None:
    """credential-leak:src/vibemaxxing/cli.py:adopt-duplicates-an-already-adopted-credential"""
    blob = credentials.credential_to_blob(make_credential())
    ctx = _ctx(tmp_home)
    ctx.port = FakeKeychain(blob=blob)
    (tmp_home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "jan@x.com", "accountUuid": "u"}})
    )
    assert cli.main(["add"], context=ctx) == 0
    assert cli.main(["add"], context=ctx) == 0
    assert store.list_aliases(tmp_home / ".vibemaxxing") == ["jan"]


def test_write_private_does_not_share_a_temp_name(tmp_path: Path) -> None:
    """credential-leak:src/vibemaxxing/fsutil.py:shared-temp-name-breaks-atomic-write"""
    from vibemaxxing import fsutil

    target = tmp_path / "account.json"
    fsutil.write_private(target, "first")
    # The pid is in the name, so no deterministic sibling is left to collide on.
    assert not (tmp_path / "account.json.tmp").exists()
    assert target.read_text() == "first"
    assert list(tmp_path.glob("*.tmp")) == []


def test_removing_an_account_takes_its_orphaned_temp_file(tmp_home: Path) -> None:
    """credential-leak:src/vibemaxxing/store.py:delete-account-leaves-orphaned-temp-credentials"""
    root = tmp_home / ".vibemaxxing"
    seed_account(root, "work")
    orphan = store.account_path(root, "work")
    orphan.with_name(orphan.name + ".tmp").write_text("a dead credential")
    store.delete_account(root, "work")
    assert not orphan.with_name(orphan.name + ".tmp").exists()


def test_human_output_survives_a_non_utf8_stream() -> None:
    """cross-platform:src/vibemaxxing/redact.py:non-utf8-stdout-crashes-human-output"""
    import io

    from vibemaxxing import redact

    stream = io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict")
    redact._write(stream, "pool 1.5 account-weeks · dry in 5h — «ok»\n")
    stream.flush()


def test_the_web_dashboard_fetches_once_per_interval_however_many_tabs() -> None:
    """memory-and-resource:src/vibemaxxing/web.py:scheduler-never-wired-into-a-surface

    Every tab polls /api/usage on its own timer, so without a cache the request
    rate is tabs x page-poll-rate and the dashboard had no floor at all.
    """
    from vibemaxxing import web
    from vibemaxxing.poll import DASHBOARD_INTERVAL_S

    calls = 0

    def produce() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"schema": 1, "n": calls}

    cache = web._Cache()
    now = 1000.0
    for _ in range(20):  # twenty tabs, same instant
        cache.get(now, produce)
    assert calls == 1

    cache.get(now + DASHBOARD_INTERVAL_S - 1, produce)
    assert calls == 1, "still inside the interval"

    cache.get(now + DASHBOARD_INTERVAL_S, produce)
    assert calls == 2, "the interval lapsed, so one fetch"
