"""AC12. The rule this repo exists to keep: no command, and no traceback out of
one, ever emits a token.

The account file is the one place the credential legitimately lives — plaintext
JSON at 0600 is the frozen at-rest decision — so it is the single exemption. The
stash, the claim, the active pointer and the raw bytes of the history database
are all scanned, and so is every byte either stream produced.
"""

from __future__ import annotations

import base64
import json
import sys
import threading
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fakes import FakeHttpClient, FakeKeychain, fixture_usage, make_credential, seed_account
from vibemaxxing import cli, credentials, store, web

# macOS resolves proxies from the system network configuration, so a Mac with a
# configured HTTP proxy would route these 127.0.0.1 requests through it.
_LOOPBACK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

SENTINEL = "VMXSENTINEL0000000000"
NOW_S = 1_757_930_000.0

FORMS = (
    SENTINEL,
    base64.b64encode(SENTINEL.encode()).decode(),
    json.dumps(SENTINEL)[1:-1],
)

# Every command the criterion names, in both modes. `usage web` is driven
# separately: it blocks on a socket rather than returning.
INVOCATIONS: tuple[list[str], ...] = (
    ["add"],
    ["add", "--json"],
    ["list"],
    ["list", "--json"],
    ["switch", "work"],
    ["switch", "work", "--json"],
    ["run", "work", "--", sys.executable, "-c", ""],
    ["run", "--json", "work", "--", sys.executable, "-c", ""],
    ["alias", "work", "job"],
    ["alias", "job", "work", "--json"],
    ["usage", "--once"],
    ["usage", "--once", "--json"],
    ["remove", "work"],
    ["remove", "work", "--json"],
)

# What to break inside each command so its failure path is exercised too. The
# forced exception carries the sentinel, which is the traceback half of AC12.
FORCED: tuple[tuple[list[str], str, str], ...] = (
    (["add"], "vibemaxxing.credentials", "parse_blob"),
    (["list"], "vibemaxxing.envelope", "collect"),
    (["switch", "work"], "vibemaxxing.store", "switch"),
    (["run", "work", "--", "true"], "vibemaxxing.store", "read_account"),
    (["alias", "work", "job"], "vibemaxxing.store", "rename_account"),
    (["usage", "--once"], "vibemaxxing.envelope", "collect"),
    (["remove", "work"], "vibemaxxing.store", "delete_account"),
)


def _context(tmp_home: Path, client: FakeHttpClient, blob: str) -> cli.Context:
    return cli.Context(
        root=tmp_home / ".vibemaxxing",
        client=client,
        port=FakeKeychain(blob=blob),
        now_s=NOW_S,
        clock=lambda: NOW_S,
        prompt=lambda _: "",
        browser=lambda _: True,
    )


def _assert_clean(text: str, where: str) -> None:
    for form in FORMS:
        assert form not in text, f"{where} leaked the sentinel"


def _scan_store(root: Path) -> int:
    scanned = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            assert path.stat().st_mode & 0o777 == 0o700, f"{path} is not 0700"
            continue
        assert path.stat().st_mode & 0o777 == 0o600, f"{path} is not 0600"
        if path.parent.name == "accounts":
            continue  # the credential at rest, by frozen product decision
        scanned += 1
        raw = path.read_bytes()
        for form in FORMS:
            assert form.encode() not in raw, f"{path} holds a token"
    return scanned


@pytest.fixture
def seeded(tmp_home: Path) -> Iterator[tuple[Path, str]]:
    root = tmp_home / ".vibemaxxing"
    credential = make_credential(access=SENTINEL, refresh=SENTINEL)
    seed_account(root, "work", credential=credential, active=True)
    blob = credentials.credential_to_blob(credential, base={"mcpOAuth": {"kept": True}})
    (tmp_home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "who@example.com", "accountUuid": "u"}})
    )
    yield root, blob


def test_no_command_or_traceback_emits_a_token(
    seeded: tuple[Path, str], tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, blob = seeded

    for argv in INVOCATIONS:
        if not store.list_aliases(root):
            seed_account(root, "work", credential=make_credential(access=SENTINEL), active=True)
        client = FakeHttpClient()
        for _ in range(3):
            client.queue_json(200, fixture_usage())
        cli.main(argv, context=_context(tmp_home, client, blob))
        captured = capsys.readouterr()
        _assert_clean(captured.out, f"{argv} stdout")
        _assert_clean(captured.err, f"{argv} stderr")

    assert _scan_store(root), "the store scan matched no files, so it proved nothing"


def test_no_forced_traceback_emits_a_token(
    seeded: tuple[Path, str],
    tmp_home: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, blob = seeded

    def boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError(f"exploded holding {SENTINEL}")

    for argv, module, attribute in FORCED:
        if not store.list_aliases(root):
            seed_account(root, "work", credential=make_credential(access=SENTINEL), active=True)
        with monkeypatch.context() as patch:
            patch.setattr(f"{module}.{attribute}", boom)
            client = FakeHttpClient()
            try:
                cli.main(argv, context=_context(tmp_home, client, blob))
            except RuntimeError as exc:
                # main() only catches VibeError, so an unclassified failure
                # reaches the excepthook it installed. That is the path a user
                # sees, and it is the one that must not print a token.
                sys.excepthook(type(exc), exc, exc.__traceback__)
        captured = capsys.readouterr()
        _assert_clean(captured.out, f"forced {argv} stdout")
        _assert_clean(captured.err, f"forced {argv} traceback")

    assert _scan_store(root) >= 0


def test_web_dashboard_serves_no_token(
    seeded: tuple[Path, str], tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, blob = seeded
    client = FakeHttpClient()
    client.queue_json(200, fixture_usage())
    ctx = _context(tmp_home, client, blob)
    recorder = web.Recorder(store.history_path(root))

    server = web.build_server(0, envelope=lambda: web.snapshot(ctx, recorder))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.socket.getsockname()[:2]
        assert host == "127.0.0.1"
        with _LOOPBACK_OPENER.open(f"http://{host}:{port}/api/usage", timeout=5) as response:
            body = response.read().decode()
        with _LOOPBACK_OPENER.open(f"http://{host}:{port}/", timeout=5) as response:
            page = response.read().decode()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        recorder.close()

    assert not thread.is_alive(), "the server thread outlived the test"
    _assert_clean(body, "/api/usage")
    _assert_clean(page, "/")
    assert json.loads(body)["accounts"][0]["alias"] == "work"

    captured = capsys.readouterr()
    _assert_clean(captured.out, "web stdout")
    _assert_clean(captured.err, "web stderr")
    assert _scan_store(root), "the store scan matched no files"


def test_a_failing_envelope_serves_no_token(tmp_home: Path) -> None:
    def raiser() -> dict[str, object]:
        raise RuntimeError(f"exploded holding {SENTINEL}")

    credentials.parse_blob(
        credentials.credential_to_blob(make_credential(access=SENTINEL))
    )  # registers the sentinel exactly as a real read would
    server = web.build_server(0, envelope=raiser)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.socket.getsockname()[:2]
        try:
            _LOOPBACK_OPENER.open(f"http://{host}:{port}/api/usage", timeout=5)
            raise AssertionError("expected a 500")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert "Traceback" not in body
    _assert_clean(body, "the 500 body")
