"""Daily ingestion of Form 4 filings from the EDGAR daily form index."""
from __future__ import annotations

import logging
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import config, db
from .edgar import NotFound, SecClient
from .parser import ParseError, parse_submission

log = logging.getLogger(__name__)

FORM_TYPES = ("4", "4/A")
ARCHIVES = "https://www.sec.gov/Archives/"
_IDX_LINE = re.compile(r"^(4|4/A)\s+.*?\s(\d+)\s+(\d{8})\s+(edgar/\S+\.txt)\s*$")
# Index dates still 404 after this many days are treated as holidays.
MISSING_AFTER_DAYS = 5


def today_et() -> date:
    return datetime.now(ZoneInfo("America/New_York")).date()


def index_url(d: date) -> str:
    q = (d.month - 1) // 3 + 1
    return f"{ARCHIVES}edgar/daily-index/{d.year}/QTR{q}/form.{d:%Y%m%d}.idx"


def parse_form_index(text: str) -> list[tuple[str, str, list[str]]]:
    """Return unique (accession, form_type, paths) for Form 4 / 4/A entries.
    Each filing appears once per filer (issuer and each owner), with one path per filer."""
    seen: dict[str, tuple[str, str, list[str]]] = {}
    for line in text.splitlines():
        if not (line.startswith("4 ") or line.startswith("4/A ")):
            continue
        m = _IDX_LINE.match(line)
        if not m:
            continue
        form_type, _cik, _date, path = m.groups()
        accession = path.rsplit("/", 1)[-1][:-4]
        seen.setdefault(accession, (accession, form_type, []))[2].append(path)
    return list(seen.values())


def ingest_date(conn: sqlite3.Connection, client: SecClient, d: date, workers: int | None = None) -> tuple[int, int] | None:
    """Ingest all Form 4 filings disseminated on date d. Returns (n_filings, n_purchases)
    or None if the index isn't available."""
    try:
        idx = client.get_text(index_url(d))
    except NotFound:
        return None
    entries = parse_form_index(idx)
    filing_date = d.isoformat()
    n_purchases = 0
    errors = 0

    def fetch(entry):
        accession, form_type, paths = entry
        for i, path in enumerate(paths):
            try:
                text = client.get_text(ARCHIVES + path)
                break
            except NotFound:
                if i == len(paths) - 1:
                    raise
        return parse_submission(text, accession, filing_date, form_type)

    with ThreadPoolExecutor(max_workers=workers or config.SEC_WORKERS) as pool:
        futures = {pool.submit(fetch, e): e for e in entries}
        for fut in as_completed(futures):
            try:
                filing = fut.result()
            except (ParseError, NotFound) as e:
                errors += 1
                log.warning("skip %s: %s", futures[fut][0], e)
                continue
            except Exception as e:  # network errors after retries
                errors += 1
                log.error("failed %s: %s", futures[fut][0], e)
                continue
            n_purchases += db.save_filing(conn, filing, source="daily")

    # A date with many fetch failures isn't marked done so it's retried next run.
    if errors > max(5, len(entries) // 50):
        conn.commit()
        raise RuntimeError(f"{d}: {errors}/{len(entries)} filings failed; will retry")
    db.log_index_date(conn, filing_date, "done", len(entries), n_purchases)
    conn.commit()
    return len(entries), n_purchases


def dates_to_ingest(conn: sqlite3.Connection, start: date, end: date, force: bool = False) -> list[date]:
    logged = {r[0] for r in conn.execute("SELECT index_date FROM ingest_log")}
    out = []
    d = start
    while d <= end:
        if d.weekday() < 5 and (force or d.isoformat() not in logged):
            out.append(d)
        d += timedelta(days=1)
    return out


def default_start(conn: sqlite3.Connection, fallback_days: int = 7) -> date:
    row = conn.execute("SELECT MAX(index_date) FROM ingest_log").fetchone()
    if row and row[0]:
        return date.fromisoformat(row[0]) + timedelta(days=1)
    return today_et() - timedelta(days=fallback_days)


def ingest_range(conn: sqlite3.Connection, start: date, end: date, force: bool = False, client: SecClient | None = None) -> None:
    client = client or SecClient()
    today = today_et()
    for d in dates_to_ingest(conn, start, end, force):
        res = ingest_date(conn, client, d)
        if res is None:
            if (today - d).days >= MISSING_AFTER_DAYS:
                db.log_index_date(conn, d.isoformat(), "missing")
                conn.commit()
                log.info("%s: no index (holiday)", d)
            else:
                log.info("%s: index not published yet", d)
            continue
        log.info("%s: %d filings, %d purchase rows", d, *res)
