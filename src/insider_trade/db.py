"""SQLite storage."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .models import Filing
from .roles import representative, role_label, role_weight

SCHEMA = """
CREATE TABLE IF NOT EXISTS filings (
    accession       TEXT PRIMARY KEY,
    form_type       TEXT NOT NULL,
    filing_date     TEXT NOT NULL,
    period          TEXT,
    issuer_cik      INTEGER NOT NULL,
    issuer_name     TEXT,
    issuer_symbol   TEXT,
    aff10b5one      INTEGER,
    owner_cik       INTEGER,   -- representative (most senior) reporting owner
    owner_name      TEXT,
    owner_title     TEXT,
    owner_role      TEXT,
    owner_weight    REAL,
    n_owners        INTEGER,
    source          TEXT
);
CREATE INDEX IF NOT EXISTS ix_filings_issuer ON filings(issuer_cik);

CREATE TABLE IF NOT EXISTS owners (
    accession   TEXT NOT NULL,
    owner_cik   INTEGER NOT NULL,
    owner_name  TEXT,
    is_director INTEGER, is_officer INTEGER, is_ten_pct INTEGER, is_other INTEGER,
    title       TEXT,
    PRIMARY KEY (accession, owner_cik)
);

CREATE TABLE IF NOT EXISTS purchases (
    accession       TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    security_title  TEXT,
    trans_date      TEXT,
    shares          REAL,
    price           REAL,
    value           REAL,
    shares_after    REAL,
    direct_indirect TEXT,
    nature          TEXT,
    PRIMARY KEY (accession, seq)
);
CREATE INDEX IF NOT EXISTS ix_purchases_date ON purchases(trans_date);

CREATE TABLE IF NOT EXISTS ingest_log (
    index_date  TEXT PRIMARY KEY,  -- EDGAR filing date covered
    status      TEXT NOT NULL,     -- done | dataset | missing
    n_filings   INTEGER,
    n_purchases INTEGER,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS dataset_log (
    quarter     TEXT PRIMARY KEY,  -- e.g. 2026q3
    n_filings   INTEGER,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS tickers (
    cik      INTEGER PRIMARY KEY,
    ticker   TEXT,
    name     TEXT,
    exchange TEXT
);

CREATE TABLE IF NOT EXISTS market_data (
    cik                 INTEGER PRIMARY KEY,
    ticker              TEXT,
    shares_outstanding  REAL,
    shares_asof         TEXT,
    shares_checked_at   TEXT,
    price               REAL,
    price_asof          TEXT,
    price_source        TEXT,
    price_ratio         REAL   -- median Form 4 price / split-unadjusted market close on trade dates
);

CREATE TABLE IF NOT EXISTS splits (
    ticker TEXT NOT NULL,
    date   TEXT NOT NULL,
    ratio  REAL NOT NULL,      -- new shares per old share (2.0 for a 2-for-1 split)
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    p = Path(path or config.db_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(market_data)")}
    if "price_ratio" not in cols:
        conn.execute("ALTER TABLE market_data ADD COLUMN price_ratio REAL")
        conn.commit()


def save_filing(conn: sqlite3.Connection, f: Filing, source: str) -> int:
    """Insert/replace a filing with its purchases. Filings without purchases are not stored.
    Returns the number of purchase rows written."""
    if not f.purchases:
        return 0
    rep = representative(f.owners)
    conn.execute("DELETE FROM purchases WHERE accession = ?", (f.accession,))
    conn.execute("DELETE FROM owners WHERE accession = ?", (f.accession,))
    conn.execute(
        """INSERT OR REPLACE INTO filings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            f.accession, f.form_type, f.filing_date, f.period, f.issuer_cik, f.issuer_name,
            f.issuer_symbol, None if f.aff10b5one is None else int(f.aff10b5one),
            rep.cik if rep else None, rep.name if rep else None, rep.title if rep else None,
            role_label(rep) if rep else None, role_weight(rep) if rep else None,
            len(f.owners), source,
        ),
    )
    conn.executemany(
        "INSERT OR REPLACE INTO owners VALUES (?,?,?,?,?,?,?,?)",
        [
            (f.accession, o.cik, o.name, int(o.is_director), int(o.is_officer),
             int(o.is_ten_pct), int(o.is_other), o.title)
            for o in f.owners
        ],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO purchases VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (
                f.accession, p.seq, p.security_title, p.trans_date, p.shares, p.price,
                (p.shares * p.price) if (p.shares is not None and p.price is not None) else None,
                p.shares_after, p.direct_indirect, p.nature,
            )
            for p in f.purchases
        ],
    )
    return len(f.purchases)


def log_index_date(conn: sqlite3.Connection, date: str, status: str, n_filings: int = 0, n_purchases: int = 0) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO ingest_log VALUES (?,?,?,?,?)",
        (date, status, n_filings, n_purchases, now_iso()),
    )


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None
