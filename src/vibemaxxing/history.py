"""The pooled sample series behind the burn-rate forecast.

One consumer, so one table and one column pair: no per-account rows, no token
columns, no request bodies. Nothing that reaches this file is a secret, which is
what makes a raw byte scan of the database a meaningful check.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Final

from vibemaxxing.models import Sample

SCHEMA_VERSION: Final = 1
RETENTION_DAYS: Final = 90
SAMPLE_LIMIT: Final = 2000

_DAY_S: Final = 86_400.0
_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS samples (
    at_s REAL PRIMARY KEY,
    pool REAL NOT NULL
)
"""


def connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        # sqlite would create it 0644; every file in the store is 0600.
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    conn = sqlite3.connect(path)
    with closing(conn.cursor()) as cur:
        cur.execute(_SCHEMA)
        # PRAGMA takes no parameter; the value is a module constant, not input.
        cur.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    return conn


def record(conn: sqlite3.Connection, *, at_s: float, pool: float) -> None:
    with closing(conn.cursor()) as cur:
        cur.execute("INSERT OR REPLACE INTO samples (at_s, pool) VALUES (?, ?)", (at_s, pool))
    conn.commit()
    # The sample being written is the clock, so a write never needs time.time().
    prune(conn, now_s=at_s)


def prune(conn: sqlite3.Connection, *, now_s: float, retention_days: int = RETENTION_DAYS) -> int:
    with closing(conn.cursor()) as cur:
        cur.execute("DELETE FROM samples WHERE at_s < ?", (now_s - retention_days * _DAY_S,))
        deleted = cur.rowcount
    conn.commit()
    return deleted


def samples(conn: sqlite3.Connection, *, since_s: float, limit: int = SAMPLE_LIMIT) -> list[Sample]:
    with closing(conn.cursor()) as cur:
        # Newest first, then reversed: a bounded read has to keep the newest
        # sample, because the forecast measures from it.
        cur.execute(
            "SELECT at_s, pool FROM samples WHERE at_s >= ? ORDER BY at_s DESC LIMIT ?",
            (since_s, limit),
        )
        rows = cur.fetchall()
    return [Sample(float(at_s), float(pool)) for at_s, pool in reversed(rows)]
