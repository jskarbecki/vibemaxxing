"""History slice: the pooled sample series and its retention window."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from vibemaxxing import history
from vibemaxxing.models import Sample

DAY_S = 86_400.0
NOW = 1_757_930_000.0


def test_history_prunes_beyond_retention(tmp_path: Path) -> None:
    conn = history.connect(tmp_path / "history.db")
    inside = [
        (NOW, 5.0),
        (NOW - DAY_S, 4.0),
        (NOW - 89 * DAY_S, 3.0),
        (NOW - 90 * DAY_S, 2.0),
    ]
    outside = [(NOW - 91 * DAY_S, 1.0), (NOW - 120 * DAY_S, 0.5)]
    # Written newest first so no write prunes a row the assertions still need.
    for at_s, pool in [*inside, *outside]:
        history.record(conn, at_s=at_s, pool=pool)

    assert history.prune(conn, now_s=NOW) == len(outside)
    assert history.samples(conn, since_s=0.0) == [
        Sample(at_s, pool) for at_s, pool in sorted(inside)
    ]
    conn.close()


def test_history_record_prunes_on_write(tmp_path: Path) -> None:
    conn = history.connect(tmp_path / "history.db")
    history.record(conn, at_s=NOW - 91 * DAY_S, pool=1.0)
    history.record(conn, at_s=NOW, pool=2.0)
    assert history.samples(conn, since_s=0.0) == [Sample(NOW, 2.0)]

    history.record(conn, at_s=NOW, pool=3.0)
    assert history.samples(conn, since_s=0.0) == [Sample(NOW, 3.0)]
    conn.close()


def test_history_samples_are_bounded_and_ordered(tmp_path: Path) -> None:
    conn = history.connect(tmp_path / "history.db")
    for i in range(10):
        history.record(conn, at_s=NOW + i, pool=float(i))

    assert history.samples(conn, since_s=NOW + 8) == [Sample(NOW + 8, 8.0), Sample(NOW + 9, 9.0)]
    assert history.samples(conn, since_s=0.0, limit=3) == [
        Sample(NOW + 7, 7.0),
        Sample(NOW + 8, 8.0),
        Sample(NOW + 9, 9.0),
    ]
    conn.close()


def test_history_connect_creates_private_file(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    conn = history.connect(path)

    assert path.stat().st_mode & 0o777 == 0o600
    with closing(conn.cursor()) as cur:
        assert cur.execute("PRAGMA user_version").fetchone()[0] == history.SCHEMA_VERSION
    conn.close()
