"""`vibe auto`: the settings, one tick, undo, and the launchd plist.

A tick is exercised end to end against the shared fakes. The usage caches under
`usage/<alias>.json` are seeded fresh, which is exactly what a real tick finds a
minute after the last one, so most cases make no HTTP request at all and the
ones that do count them.
"""

from __future__ import annotations

import json
import plistlib
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.fakes import FakeHttpClient, FakeKeychain, make_credential, seed_account
from vibemaxxing import auto, cli, credentials, oauth, store, usage
from vibemaxxing.credentials import Identity, credential_to_blob
from vibemaxxing.errors import (
    NeedsLoginError,
    NetworkError,
    NotFoundError,
    UsageError,
    VibeError,
)
from vibemaxxing.httpclient import HTTPError
from vibemaxxing.models import AccountState
from vibemaxxing.pool import LastSwitch, Settings

NOW_S = 1_757_930_000.0
ADDED_AT = 1_757_930_000.0  # tests.fakes.seed_account's default


def _iso(at_s: float) -> str:
    return datetime.fromtimestamp(at_s, UTC).isoformat()


def payload(
    session: int, weekly: int, *, now: float = NOW_S, scoped: int | None = None
) -> dict[str, object]:
    limits: list[dict[str, object]] = [
        {
            "kind": "session",
            "percent": session,
            "severity": "normal",
            "resets_at": _iso(now + 3 * 3600),
        },
        {
            "kind": "weekly_all",
            "percent": weekly,
            "severity": "normal",
            "resets_at": _iso(now + 2 * 86400),
        },
    ]
    if scoped is not None:
        limits.append(
            {
                "kind": "weekly_scoped",
                "percent": scoped,
                "severity": "normal",
                "resets_at": _iso(now + 2 * 86400),
                "scope": {"model": {"display_name": "Fable"}},
            }
        )
    return {"limits": limits}


def cache(
    root: Path, alias: str, body: dict[str, object], *, fetched_at: float = NOW_S - 10.0
) -> None:
    store.write_usage_cache(
        root,
        alias,
        store.UsageCache(ADDED_AT, fetched_at, body, retry_at=None, failures=0, message=None),
    )


@dataclass
class World:
    root: Path
    port: FakeKeychain
    client: FakeHttpClient = field(default_factory=FakeHttpClient)
    notes: list[str] = field(default_factory=list)

    def context(self) -> cli.Context:
        return cli.Context(
            root=self.root,
            client=self.client,
            port=self.port,
            now_s=NOW_S,
            clock=lambda: NOW_S,
            prompt=lambda _: "",
            browser=lambda _: True,
            notify=self.notes.append,
        )

    def tick(self, now_s: float = NOW_S) -> auto.TickResult:
        return auto.tick(
            self.root, client=self.client, port=self.port, now_s=now_s, notify=self.notes.append
        )


def world(
    tmp_home: Path,
    usage_by_alias: dict[str, tuple[int, int]],
    *,
    live: str = "a",
    on: bool = True,
    seed_cache: bool = True,
) -> World:
    """Every alias stored with uuid u<alias> and a fresh cache; `live` is what Claude Code runs."""
    root = tmp_home / ".vibemaxxing"
    for alias, (session, weekly) in usage_by_alias.items():
        seed_account(
            root,
            alias,
            identity=Identity(None, f"u{alias}", None, None, None, None, None),
            credential=make_credential(access=f"{alias}-file", refresh=f"{alias}-rt"),
            active=alias == live,
        )
        if seed_cache:
            cache(root, alias, payload(session, weekly))
    (tmp_home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"accountUuid": f"u{live}"}})
    )
    blob = credential_to_blob(
        make_credential(access=f"{live}-live", refresh=f"{live}-live-rt"),
        base={"mcpOAuth": {"kept": True}},
    )
    if on:
        auto.write_settings(root, Settings(auto_switch=True))
    return World(root, FakeKeychain(blob=blob))


# --- settings -----------------------------------------------------------------


def test_settings_default_to_off_and_a_missing_file_is_not_an_error(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"

    assert auto.read_settings(root) == Settings()
    assert Settings() == Settings(auto_switch=False, threshold=99, cooldown_s=900, excluded=())


def test_settings_are_written_owner_only_in_the_documented_shape(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"

    auto.write_settings(root, Settings(auto_switch=True, excluded=("b",)))

    path = auto.settings_path(root)
    assert path == root / "settings.json"
    assert json.loads(path.read_text()) == {
        "schema": 1,
        "auto_switch": True,
        "threshold": 99,
        "cooldown_s": 900,
        "excluded": ["b"],
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert auto.read_settings(root) == Settings(auto_switch=True, excluded=("b",))


@pytest.mark.parametrize(
    "raw",
    [
        '{"threshold": "x"}',
        '{"schema": 1, "threshold": "x"}',
        '{"schema": 1, "threshold": 49}',
        '{"schema": 1, "threshold": 101}',
        '{"schema": 1, "threshold": true}',
        '{"schema": 1, "threshold": 99.5}',
        '{"schema": 1, "auto_switch": "yes"}',
        '{"schema": 1, "cooldown_s": -1}',
        '{"schema": 1, "cooldown_s": "soon"}',
        '{"schema": 1, "excluded": "b"}',
        '{"schema": 1, "excluded": [1]}',
        '{"schema": 1, "excluded": ["../etc"]}',
        '{"schema": 2, "auto_switch": true}',
        "[1, 2]",
        "{ not json",
        "",
    ],
)
def test_an_invalid_settings_file_means_the_defaults(tmp_home: Path, raw: str) -> None:
    root = tmp_home / ".vibemaxxing"
    root.mkdir()
    auto.settings_path(root).write_text(raw)

    # All of it, not the valid half: a hand-edited file that is wrong somewhere must
    # leave auto-switch off rather than switch on a guess about what was meant.
    assert auto.read_settings(root) == Settings()


def test_settings_missing_members_take_their_defaults(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"
    root.mkdir()
    auto.settings_path(root).write_text('{"schema": 1, "auto_switch": true, "threshold": 90}')

    assert auto.read_settings(root) == Settings(auto_switch=True, threshold=90)


# --- one tick -----------------------------------------------------------------


def test_tick_switches_and_records(tmp_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    w = world(tmp_home, {"a": (99, 30), "b": (10, 10)})

    assert cli.main(["auto", "tick"], context=w.context()) == 0

    line = capsys.readouterr().out.strip()
    assert line.startswith("switched a -> b")
    assert w.port.blob is not None
    assert credentials.parse_blob(w.port.blob).access_token.reveal() == "b-file"
    assert json.loads(w.port.blob)["mcpOAuth"] == {"kept": True}, "siblings survive an auto switch"
    assert store.read_active(w.root) == "b"
    last = auto.read_last(w.root)
    assert last is not None
    assert (last.from_alias, last.to_alias, last.at_s) == ("a", "b", NOW_S)
    assert last.reason in line
    assert json.loads((w.root / "auto.json").read_text())["from"] == "a"
    assert w.notes == [line], "one notification, naming the switch"
    assert w.client.requests == [], "the fresh caches answered; no request was needed"


def test_tick_remembers_when_the_account_it_left_resets(tmp_home: Path) -> None:
    w = world(tmp_home, {"a": (50, 99), "b": (10, 10)})

    w.tick()

    last = auto.read_last(w.root)
    assert last is not None
    # a was left over its weekly row, which resets two days out: the session row at 50%
    # is not what limited it.
    assert last.until_s == pytest.approx(NOW_S + 2 * 86400)


def test_tick_survives_claim_held(tmp_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    w = world(tmp_home, {"a": (99, 30), "b": (10, 10)})
    before = w.port.blob
    assert store.claim_refresh(w.root, "b", now_s=1e12)  # a refresh of b is in flight

    assert cli.main(["auto", "tick"], context=w.context()) == 0

    line = capsys.readouterr().out.strip()
    assert line.startswith("stay:")
    assert "in progress" in line
    assert w.port.blob == before
    assert store.read_active(w.root) == "a"
    assert auto.read_last(w.root) is None
    assert w.notes == []


def test_tick_stays_when_off_and_asks_nothing(tmp_home: Path) -> None:
    w = world(tmp_home, {"a": (100, 100), "b": (0, 0)}, on=False, seed_cache=False)

    result = w.tick()

    assert (result.switched, result.line) == (False, "stay: auto-switch is off")
    assert w.client.requests == [] and w.port.log == []


def test_tick_stays_when_it_cannot_tell_who_is_live(tmp_home: Path) -> None:
    w = world(tmp_home, {"a": (100, 100), "b": (0, 0)})
    (tmp_home / ".claude.json").unlink()

    assert w.tick().line == "stay: live account unknown"
    assert w.client.requests == []


def test_tick_repairs_an_active_pointer_that_is_not_the_live_account(tmp_home: Path) -> None:
    # `a` is named active and never refreshed, while Claude Code runs as `b`. Staying here
    # left `a` to lapse and `b` to be refreshed behind Claude Code's back, for good.
    w = world(tmp_home, {"a": (99, 99), "b": (10, 10)}, live="b")
    store.write_active(w.root, "a")
    result = w.tick()

    assert store.read_active(w.root) == "b"
    assert result.switched is False
    assert w.port.blob is not None
    assert credentials.parse_blob(w.port.blob).access_token.reveal() == "b-live"


def test_tick_stays_with_the_repair_command_when_the_repair_is_refused(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world(tmp_home, {"a": (10, 10), "b": (99, 99)}, live="b")
    store.write_active(w.root, "a")

    def refuse(*_: object, **__: object) -> None:
        raise UsageError("another vibe switch just ran", "vibe switch b")

    monkeypatch.setattr(store, "switch", refuse)

    result = w.tick()

    assert result.switched is False
    assert result.line.startswith("stay: Claude Code runs as b but vibe's active account is a")
    assert "repairing it failed: another vibe switch just ran" in result.line
    assert "vibe switch b" in result.line
    assert w.client.requests == []


def test_tick_stays_below_the_threshold(tmp_home: Path) -> None:
    w = world(tmp_home, {"a": (98, 98), "b": (0, 0)})

    assert w.tick().line == "stay: a is below 99%"
    assert w.notes == []


def test_tick_never_switches_onto_a_dead_account(tmp_home: Path) -> None:
    w = world(tmp_home, {"a": (99, 30), "b": (0, 0), "c": (0, 0)})
    dead = store.read_account(w.root, "b")
    store.write_account(
        w.root,
        store.Account(
            "b",
            make_credential(refresh_expires_at_ms=1),
            dead.identity,
            AccountState.OK,
            None,
            dead.added_at,
        ),
    )
    store.write_account(
        w.root,
        store.Account(
            "c",
            make_credential(),
            dead.identity,
            AccountState.NEEDS_LOGIN,
            "rejected",
            dead.added_at,
        ),
    )

    result = w.tick()

    assert result.line == "stay: no account with headroom"
    assert store.read_active(w.root) == "a"


def test_tick_never_switches_onto_a_stale_account(tmp_home: Path) -> None:
    w = world(tmp_home, {"a": (99, 30), "b": (0, 0)})
    cache(w.root, "b", payload(0, 0), fetched_at=NOW_S - 200.0)
    w.client.queue(HTTPError(429, b""))  # the refetch of b is refused: the old answer stays

    assert w.tick().line == "stay: no account with headroom"


def test_tick_never_switches_onto_an_account_it_could_not_identify_afterwards(
    tmp_home: Path,
) -> None:
    w = world(tmp_home, {"a": (99, 30), "b": (0, 0)})
    seed_account(w.root, "b", credential=make_credential(access="b-file"))  # no uuid stored

    assert w.tick().line == "stay: no account with headroom"
    assert store.read_active(w.root) == "a"


def test_tick_stays_when_the_live_account_changes_while_it_checks(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # collect and the refresh can take seconds; a person who runs `vibe switch c` in that
    # time must not be overridden by a decision made about a.
    w = world(tmp_home, {"a": (99, 30), "b": (10, 10), "c": (50, 50)})
    real = oauth.ensure_usable

    def then_someone_switches(
        root: Path, alias: str, client: FakeHttpClient, *, now_ms: int
    ) -> None:
        real(root, alias, client, now_ms=now_ms)
        store.write_active(w.root, "c")

    monkeypatch.setattr(oauth, "ensure_usable", then_someone_switches)
    before = w.port.blob

    result = w.tick()

    assert result.switched is False
    assert result.line.startswith("stay: the live account changed")
    assert w.port.blob == before
    assert store.read_active(w.root) == "c"
    assert auto.read_last(w.root) is None and w.notes == []


@pytest.mark.parametrize(
    "refusal",
    [
        NeedsLoginError('the refresh token for "b" was rejected', "vibe add b"),
        NetworkError('could not confirm that "b" is usable right now (transient)', "vibe switch b"),
    ],
)
def test_tick_stays_when_the_target_turns_out_dead_or_unconfirmable(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, refusal: VibeError
) -> None:
    w = world(tmp_home, {"a": (99, 30), "b": (10, 10)})
    asked: list[str] = []

    def refuse(root: Path, alias: str, client: FakeHttpClient, *, now_ms: int) -> None:
        asked.append(alias)
        raise refusal

    monkeypatch.setattr(oauth, "ensure_usable", refuse)
    before = w.port.blob

    result = w.tick()

    assert asked == ["b"], "the tick proves the chosen target alive before it switches"
    assert result.line == f"stay: {refusal.message}"
    assert w.port.blob == before and "keychain.write" not in w.port.log
    assert store.read_active(w.root) == "a"
    assert auto.read_last(w.root) is None


def test_tick_stays_when_the_target_was_removed_in_between(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world(tmp_home, {"a": (99, 30), "b": (10, 10)})

    def gone(*args: object, **kwargs: object) -> None:
        raise NotFoundError('there is no account named "b"', "vibe list")

    monkeypatch.setattr(store, "switch", gone)

    assert w.tick().line == 'stay: there is no account named "b"'


def test_tick_still_reports_a_switch_it_could_not_record(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world(tmp_home, {"a": (99, 30), "b": (10, 10)})

    def disk_full(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(auto, "write_last", disk_full)

    result = w.tick()

    # The switch landed; reporting a crash instead would hide that and lose the notification.
    assert result.switched is True
    assert result.line.startswith("switched a -> b")
    assert "not recorded" in result.line
    assert store.read_active(w.root) == "b" and w.notes == [result.line]


def test_tick_respects_the_cooldown(tmp_home: Path) -> None:
    w = world(tmp_home, {"a": (99, 30), "b": (10, 10)})
    auto.write_last(w.root, LastSwitch("b", "a", NOW_S - 100.0, "earlier", None))

    line = w.tick().line

    assert line.startswith("stay: cooldown")
    assert store.read_active(w.root) == "a"


def test_tick_does_not_retry_a_switch_the_guard_refuses(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Someone runs `claude /login` as an account vibe never stored in the instant after the
    # tick's last look at who is live (its first look is before it collects, its second just
    # before it switches). The guard in store.switch refuses; the tick says so, once.
    w = world(tmp_home, {"a": (99, 30), "b": (10, 10)})
    stranger = credential_to_blob(make_credential(access="stranger", refresh="stranger-rt"))
    real = store.live_alias
    looks = 0

    def live_then_someone_logs_in(root: Path, port: FakeKeychain) -> str | None:
        nonlocal looks
        alias = real(root, port)
        looks += 1
        if looks == 2:
            (tmp_home / ".claude.json").write_text(
                json.dumps({"oauthAccount": {"accountUuid": "stranger"}})
            )
            port.blob = stranger
        return alias

    monkeypatch.setattr(store, "live_alias", live_then_someone_logs_in)

    result = w.tick()

    assert result.switched is False
    assert result.line.startswith("stay: Claude Code is logged in as stranger")
    assert w.port.blob == stranger, "the stranger's login is still there"
    assert "keychain.write" not in w.port.log
    assert auto.read_last(w.root) is None
    assert w.notes == []


# --- faster polling of the live account -----------------------------------------


def _usage_requests(w: World, token: str) -> int:
    return sum(
        1
        for request in w.client.requests
        if request.url == usage.USAGE_URL and request.headers["Authorization"] == f"Bearer {token}"
    )


def test_tick_fast_polls_live_account_at_most_once_a_minute(tmp_home: Path) -> None:
    w = world(tmp_home, {"a": (92, 30), "b": (10, 10)}, seed_cache=False)
    cache(w.root, "b", payload(10, 10), fetched_at=NOW_S - 10.0)
    for _ in range(3):
        w.client.queue_json(200, payload(92, 30))

    first = w.tick(NOW_S)
    assert _usage_requests(w, "a-live") == 1, "no cache yet, so the first tick fetches"
    w.tick(NOW_S + 30.0)
    assert _usage_requests(w, "a-live") == 1, "92% is hot, but 30 s is inside the minute"
    w.tick(NOW_S + 61.0)

    assert _usage_requests(w, "a-live") == 2
    assert first.line == "stay: a is below 99%"
    assert _usage_requests(w, "b-file") == 0, "only the live account is polled faster"


def test_tick_does_not_fast_poll_below_ninety(tmp_home: Path) -> None:
    w = world(tmp_home, {"a": (89, 30), "b": (10, 10)}, seed_cache=False)
    cache(w.root, "b", payload(10, 10))
    w.client.queue_json(200, payload(89, 30))

    w.tick(NOW_S)
    w.tick(NOW_S + 61.0)

    assert _usage_requests(w, "a-live") == 1, "89% waits the usual 170 s"


def test_fast_poll_still_obeys_a_backoff(tmp_home: Path) -> None:
    w = world(tmp_home, {"a": (95, 30), "b": (10, 10)}, seed_cache=False)
    cache(w.root, "b", payload(10, 10))
    store.write_usage_cache(
        w.root,
        "a",
        store.UsageCache(
            ADDED_AT,
            NOW_S - 90.0,
            payload(95, 30),
            retry_at=NOW_S + 300.0,
            failures=1,
            message="rate limited - next try 00:00",
        ),
    )

    w.tick(NOW_S)

    assert w.client.requests == [], "a 429 backoff outranks the faster cadence"


# --- undo ---------------------------------------------------------------------


def test_auto_undo_restores_previous(tmp_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    w = world(tmp_home, {"a": (99, 30), "b": (10, 10)})
    w.tick()
    assert store.read_active(w.root) == "b"
    capsys.readouterr()

    assert cli.main(["auto", "undo"], context=w.context()) == 0

    assert w.port.blob is not None
    assert credentials.parse_blob(w.port.blob).access_token.reveal() == "a-live"
    assert store.read_active(w.root) == "a"
    last = auto.read_last(w.root)
    assert last is not None and (last.from_alias, last.to_alias) == ("b", "a")
    assert last.reason == "undo", "an undo is a switch: the cooldown starts again"
    assert 'back on "a"' in capsys.readouterr().out


def test_auto_undo_with_nothing_to_undo_names_the_next_step(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    w = world(tmp_home, {"a": (10, 10), "b": (10, 10)})

    assert cli.main(["auto", "undo"], context=w.context()) == 2

    assert "vibe auto status" in capsys.readouterr().err


# --- the commands that edit settings --------------------------------------------


def test_on_off_exclude_include_edit_the_settings(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    w = world(tmp_home, {"a": (10, 10), "b": (10, 10)}, on=False)
    ctx = w.context()

    assert cli.main(["auto", "on"], context=ctx) == 0
    assert auto.read_settings(w.root).auto_switch is True
    assert cli.main(["auto", "exclude", "b"], context=ctx) == 0
    assert cli.main(["auto", "exclude", "b"], context=ctx) == 0
    assert auto.read_settings(w.root).excluded == ("b",)
    assert cli.main(["auto", "include", "b"], context=ctx) == 0
    assert auto.read_settings(w.root).excluded == ()
    assert cli.main(["auto", "off"], context=ctx) == 0
    assert auto.read_settings(w.root).auto_switch is False

    capsys.readouterr()
    assert cli.main(["auto", "exclude", "ghost"], context=ctx) == 4
    assert "vibe list" in capsys.readouterr().err


def test_editing_an_unreadable_settings_file_is_refused_not_overwritten(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Reading it as the defaults is right for `vibe list`. Writing the defaults back over
    # it is not: it would silently drop a hand-written exclusion and then switch on.
    w = world(tmp_home, {"a": (10, 10), "b": (10, 10)}, on=False)
    path = auto.settings_path(w.root)
    path.write_text('{"schema": 1, "excluded": ["b"], "cooldown_s": "900"}')
    before = path.read_bytes()

    for argv in (["on"], ["off"], ["exclude", "a"], ["include", "b"]):
        assert cli.main(["auto", *argv], context=w.context()) == 2, argv
        err = capsys.readouterr().err
        assert str(path) in err and f"rm {path}" in err, argv
    assert path.read_bytes() == before
    assert auto.read_settings(w.root) == Settings(), "and reading it is still the defaults"


def test_renaming_an_account_keeps_it_excluded_and_remembers_its_switch(
    tmp_home: Path,
) -> None:
    w = world(tmp_home, {"a": (99, 30), "b": (10, 10)})
    auto.write_settings(w.root, Settings(auto_switch=True, excluded=("b",)))
    auto.write_last(w.root, LastSwitch("b", "a", NOW_S - 5000.0, "b session at 99%", NOW_S + 3600))

    assert cli.main(["alias", "b", "bee"], context=w.context()) == 0

    assert auto.read_settings(w.root).excluded == ("bee",)
    last = auto.read_last(w.root)
    assert last is not None and (last.from_alias, last.to_alias) == ("bee", "a")
    assert w.tick().line == "stay: no account with headroom", "the fence moved with the name"


def test_a_last_switch_with_an_absurd_time_is_no_record_and_breaks_nothing(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_home / ".vibemaxxing"
    root.mkdir()
    for at in ("1e12", "1e300", "-1e300"):
        auto.last_path(root).write_text(
            f'{{"schema": 1, "from": "a", "to": "b", "at": {at}, "reason": "r", "until": null}}'
        )
        assert auto.read_last(root) is None, at
    auto.last_path(root).write_text(
        '{"schema": 1, "from": "a", "to": "b", "at": 1757930000, "reason": "r", "until": 1e300}'
    )
    last = auto.read_last(root)
    assert last is not None and last.until_s is None, "an unusable `until` is just unknown"

    seed_account(root, "work", active=True)
    auto.last_path(root).write_text(
        '{"schema": 1, "from": "a", "to": "b", "at": 1e12, "reason": "r", "until": null}'
    )
    client = FakeHttpClient()
    client.queue_json(200, payload(10, 10))
    ctx = cli.Context(
        root=root,
        client=client,
        port=FakeKeychain(),
        now_s=NOW_S,
        prompt=lambda _: "",
        browser=lambda _: True,
    )
    assert cli.main(["--json", "list"], context=ctx) == 0
    assert json.loads(capsys.readouterr().out)["auto_switch"]["last_switch"] is None


def test_every_auto_command_speaks_json(tmp_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    w = world(tmp_home, {"a": (10, 10), "b": (40, 40)})
    ctx = w.context()

    for argv in (["on"], ["off"], ["exclude", "b"], ["include", "b"], ["tick"], ["status"]):
        capsys.readouterr()
        assert cli.main(["auto", *argv, "--json"], context=ctx) == 0, argv
        out = json.loads(capsys.readouterr().out)
        assert out["schema"] == 1 and out["ok"] is True, argv
        assert out["action"] == f"auto {argv[0]}", argv


def test_undo_install_and_uninstall_speak_json_too(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    _no_subprocess(monkeypatch)
    w = world(tmp_home, {"a": (10, 10), "b": (10, 10)})
    auto.write_last(w.root, LastSwitch("b", "a", NOW_S - 5000.0, "b session at 99%", None))

    for command in ("undo", "install", "uninstall"):
        capsys.readouterr()
        assert cli.main(["auto", command, "--json"], context=w.context()) == 0, command
        out = json.loads(capsys.readouterr().out)
        assert (out["schema"], out["ok"], out["action"]) == (1, True, f"auto {command}")
    assert store.read_active(w.root) == "b", "undo went back to the account the record left"


def test_status_says_who_is_live_and_where_a_switch_would_go(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    w = world(tmp_home, {"a": (10, 10), "b": (40, 40), "c": (5, 5)})
    auto.write_last(w.root, LastSwitch("c", "a", NOW_S - 5000.0, "c session at 99%", None))

    assert cli.main(["auto", "status", "--json"], context=w.context()) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["enabled"] is True
    assert out["threshold"] == 99 and out["cooldown_s"] == 900 and out["excluded"] == []
    assert (out["live"], out["active"], out["next"]) == ("a", "a", "c")
    assert out["last_switch"] == {
        "from": "c",
        "to": "a",
        "at": _iso(NOW_S - 5000.0),
        "reason": "c session at 99%",
    }

    capsys.readouterr()
    assert cli.main(["auto", "status"], context=w.context()) == 0
    text = capsys.readouterr().out
    assert text.startswith(
        "auto-switch  on\nthreshold    99%  cooldown 900s\nexcluded     none\nlive         a\n"
    )
    assert "last switch  c -> a at " in text and "(c session at 99%)" in text
    assert text.endswith("next         c\n")


def test_status_exactly_when_nothing_has_happened(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    w = world(tmp_home, {"a": (10, 10), "b": (40, 40), "c": (5, 5)})
    auto.write_settings(w.root, Settings(auto_switch=True, excluded=("b",)))

    assert cli.main(["auto", "status"], context=w.context()) == 0

    assert capsys.readouterr().out == (
        "auto-switch  on\n"
        "threshold    99%  cooldown 900s\n"
        "excluded     b\n"
        "live         a\n"
        "last switch  none\n"
        "next         c\n"
    )


def test_status_says_how_to_repair_a_stale_pointer(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    w = world(tmp_home, {"a": (10, 10), "b": (99, 99)}, live="b")
    store.write_active(w.root, "a")

    assert cli.main(["auto", "status"], context=w.context()) == 0

    out = capsys.readouterr().out
    assert "live         b  (vibe's active account is a - run: vibe switch b)\n" in out
    assert out.endswith("next         none\n"), "it does not poll while the pointer is wrong"
    assert w.client.requests == []


def test_status_with_nothing_known_is_still_an_answer(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_home / ".vibemaxxing"
    ctx = cli.Context(
        root=root,
        client=FakeHttpClient(),
        port=FakeKeychain(),
        now_s=NOW_S,
        prompt=lambda _: "",
        browser=lambda _: True,
    )

    assert cli.main(["auto", "status", "--json"], context=ctx) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["enabled"] is False
    assert (out["live"], out["next"], out["last_switch"]) == (None, None, None)


# --- launchd ------------------------------------------------------------------


def _no_subprocess(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    seen: list[object] = []

    def refuse(*args: object, **kwargs: object) -> None:
        seen.append(args)
        raise AssertionError(f"a subprocess was started: {args!r}")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    return seen


def test_auto_install_writes_plist_only(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    seen = _no_subprocess(monkeypatch)
    w = world(tmp_home, {"a": (10, 10)})

    assert cli.main(["auto", "install"], context=w.context()) == 0

    plist = tmp_home / "Library" / "LaunchAgents" / "de.intra-ai.vibemaxxing-auto.plist"
    assert plist.exists()
    body = plistlib.loads(plist.read_bytes())
    assert body["StartInterval"] == 60
    assert body["Label"] == "de.intra-ai.vibemaxxing-auto"
    assert body["ProgramArguments"][-2:] == ["auto", "tick"]
    assert body["ProgramArguments"][0] == sys.executable
    assert seen == [], "install must not run launchctl; the operator loads the job"
    out = capsys.readouterr().out
    assert "launchctl bootstrap gui/" in out
    assert str(plist) in out


def test_auto_uninstall_removes_the_plist_and_names_the_unload(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    seen = _no_subprocess(monkeypatch)
    w = world(tmp_home, {"a": (10, 10)})
    assert cli.main(["auto", "install"], context=w.context()) == 0
    capsys.readouterr()

    assert cli.main(["auto", "uninstall"], context=w.context()) == 0

    assert not (
        tmp_home / "Library" / "LaunchAgents" / "de.intra-ai.vibemaxxing-auto.plist"
    ).exists()
    assert "launchctl bootout gui/" in capsys.readouterr().out
    assert seen == []
    assert cli.main(["auto", "uninstall"], context=w.context()) == 0, "already gone is not an error"


def test_install_is_macos_only(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    w = world(tmp_home, {"a": (10, 10)})

    for command in ("install", "uninstall"):
        assert cli.main(["auto", command], context=w.context()) == 2
        assert "macOS" in capsys.readouterr().err
    assert not (tmp_home / "Library").exists()


# --- the notification ---------------------------------------------------------


def test_notify_passes_the_message_as_an_argument_never_as_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    hostile = 'a" & (do shell script "x") & "'

    auto.notify_macos(hostile)

    (argv,) = calls
    assert argv[0] == "/usr/bin/osascript"
    assert hostile in argv, "the message travels as its own argv entry"
    assert all(hostile not in part for part in argv if part != hostile)


def test_notify_swallows_every_failure_and_is_silent_off_macos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    def broken(*args: object, **kwargs: object) -> None:
        raise OSError("no such binary")

    monkeypatch.setattr(subprocess, "run", broken)
    auto.notify_macos("switched a -> b")

    def timed_out(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired("osascript", 5)

    monkeypatch.setattr(subprocess, "run", timed_out)
    auto.notify_macos("switched a -> b")

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("ran off macOS"))
    auto.notify_macos("switched a -> b")


def test_last_switch_that_is_unreadable_is_no_record(tmp_home: Path) -> None:
    root = tmp_home / ".vibemaxxing"
    root.mkdir()
    for raw in ("", "{ nope", '{"schema": 2}', '{"schema": 1, "from": "a"}', "[]"):
        (root / "auto.json").write_text(raw)
        assert auto.read_last(root) is None, raw
