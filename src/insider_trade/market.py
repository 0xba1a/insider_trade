"""Tickers, shares outstanding (SEC XBRL) and prices (yfinance) for market-cap normalization."""
from __future__ import annotations

import logging
import math
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

from . import config, db
from .edgar import NotFound, SecClient

log = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
CONCEPT_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/{concept}.json"
# Tried in order. Multi-class issuers often only report the dei value per class (dimensional),
# which the API omits, so fall back to us-gaap totals.
SHARE_CONCEPTS = [
    ("dei/EntityCommonStockSharesOutstanding", True),
    ("us-gaap/CommonStockSharesOutstanding", False),
    ("us-gaap/WeightedAverageNumberOfSharesOutstandingBasic", False),
]
MAX_SHARES_AGE_DAYS = 730


def refresh_tickers(conn: sqlite3.Connection, client: SecClient) -> int:
    data = client.get_json(TICKERS_URL)
    fields = data["fields"]
    rows = {}
    for rec in data["data"]:
        r = dict(zip(fields, rec))
        # The first listing per CIK is the primary share class.
        rows.setdefault(int(r["cik"]), (int(r["cik"]), r["ticker"], r["name"], r.get("exchange")))
    conn.execute("DELETE FROM tickers")
    conn.executemany("INSERT INTO tickers VALUES (?,?,?,?)", rows.values())
    db.set_meta(conn, "tickers_updated_at", db.now_iso())
    conn.commit()
    return len(rows)


def latest_shares(facts: list[dict], sum_classes: bool) -> tuple[float, str] | None:
    facts = [f for f in facts if f.get("val") is not None and f.get("end")]
    if not facts:
        return None
    end = max(f["end"] for f in facts)
    at_end = [f for f in facts if f["end"] == end]
    filed = max(f.get("filed", "") for f in at_end)
    latest = [f for f in at_end if f.get("filed", "") == filed]
    accn = latest[0].get("accn")
    same = [f for f in latest if f.get("accn") == accn]
    if sum_classes:
        # Several values in one filing for the same date are separate share classes.
        val = float(sum(f["val"] for f in same))
    else:
        val = float(same[0]["val"])
    return val, end


def fetch_shares_outstanding(client: SecClient, cik: int) -> tuple[float, str] | None:
    cutoff = (date.today() - timedelta(days=MAX_SHARES_AGE_DAYS)).isoformat()
    for concept, sum_classes in SHARE_CONCEPTS:
        try:
            data = client.get_json(CONCEPT_URL.format(cik=cik, concept=concept))
        except NotFound:
            continue
        res = latest_shares(data.get("units", {}).get("shares", []), sum_classes)
        if res and res[0] > 0 and res[1] >= cutoff:
            return res
    return None


def active_issuers(conn: sqlite3.Connection, days: int) -> list[sqlite3.Row]:
    since = (date.today() - timedelta(days=days)).isoformat()
    return conn.execute(
        """
        SELECT DISTINCT f.issuer_cik AS cik, COALESCE(t.ticker, f.issuer_symbol) AS ticker
        FROM purchases p JOIN filings f USING (accession)
        LEFT JOIN tickers t ON t.cik = f.issuer_cik
        WHERE p.trans_date >= ? AND COALESCE(t.ticker, f.issuer_symbol, '') != ''
        """,
        (since,),
    ).fetchall()


def refresh_shares(conn: sqlite3.Connection, client: SecClient, issuers: list[sqlite3.Row], max_age_days: int = 30, force: bool = False) -> int:
    stale_before = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    checked = {
        r["cik"]: r["shares_checked_at"]
        for r in conn.execute("SELECT cik, shares_checked_at FROM market_data")
    }
    todo = [r for r in issuers if force or not checked.get(r["cik"]) or checked[r["cik"]] < stale_before]
    log.info("shares outstanding: %d issuers to refresh", len(todo))

    def work(r):
        try:
            return r, fetch_shares_outstanding(client, r["cik"])
        except Exception as e:
            log.warning("shares %s (%s): %s", r["ticker"], r["cik"], e)
            return r, "error"

    n = 0
    with ThreadPoolExecutor(max_workers=config.SEC_WORKERS) as pool:
        for r, res in pool.map(work, todo):
            if res == "error":
                continue
            shares, asof = res if res else (None, None)
            conn.execute(
                """INSERT INTO market_data (cik, ticker, shares_outstanding, shares_asof, shares_checked_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(cik) DO UPDATE SET ticker=excluded.ticker,
                     shares_outstanding=excluded.shares_outstanding, shares_asof=excluded.shares_asof,
                     shares_checked_at=excluded.shares_checked_at""",
                (r["cik"], r["ticker"], shares, asof, db.now_iso()),
            )
            n += 1
            if n % 200 == 0:
                conn.commit()
    conn.commit()
    return n


def _yahoo_symbol(ticker: str) -> str:
    return ticker.replace(".", "-").upper()


def fetch_prices(tickers: list[str], chunk: int = 200) -> dict[str, tuple[float, str]]:
    import yfinance as yf  # imported lazily; slow import

    out: dict[str, tuple[float, str]] = {}
    for i in range(0, len(tickers), chunk):
        batch = tickers[i : i + chunk]
        symbols = [_yahoo_symbol(t) for t in batch]
        try:
            df = yf.download(symbols, period="7d", progress=False, auto_adjust=False, threads=True)
        except Exception as e:
            log.warning("yfinance batch failed: %s", e)
            continue
        if df is None or df.empty:
            continue
        close = df["Close"]
        if not hasattr(close, "columns"):  # single symbol returns a Series
            close = close.to_frame(symbols[0])
        close = close.ffill()
        last_idx = close.index[-1]
        for t, s in zip(batch, symbols):
            if s in close.columns:
                v = close[s].iloc[-1]
                if v is not None and not math.isnan(v) and v > 0:
                    out[t] = (float(v), str(last_idx.date()))
    return out


def refresh_prices(conn: sqlite3.Connection, issuers: list[sqlite3.Row]) -> tuple[int, int]:
    """Update prices from yfinance; fall back to the latest Form 4 purchase price.
    Returns (n_yahoo, n_fallback)."""
    tickers = sorted({r["ticker"] for r in issuers})
    prices = fetch_prices(tickers) if tickers else {}
    fallback = {
        r["issuer_cik"]: (r["price"], r["trans_date"])
        for r in conn.execute(
            """
            SELECT f.issuer_cik, p.price, p.trans_date FROM purchases p JOIN filings f USING (accession)
            WHERE p.price > 0 AND p.shares > 0
            ORDER BY p.trans_date
            """
        )
    }  # later rows overwrite earlier ones -> latest purchase price
    n_y = n_f = 0
    for r in issuers:
        if r["ticker"] in prices:
            price, asof = prices[r["ticker"]]
            source = "yahoo"
            n_y += 1
        elif r["cik"] in fallback:
            price, asof = fallback[r["cik"]]
            source = "form4"
            n_f += 1
        else:
            continue
        conn.execute(
            """INSERT INTO market_data (cik, ticker, price, price_asof, price_source) VALUES (?,?,?,?,?)
               ON CONFLICT(cik) DO UPDATE SET ticker=excluded.ticker, price=excluded.price,
                 price_asof=excluded.price_asof, price_source=excluded.price_source""",
            (r["cik"], r["ticker"], price, asof, source),
        )
    conn.commit()
    return n_y, n_f


def refresh_market(conn: sqlite3.Connection, days: int = 400, force: bool = False, client: SecClient | None = None) -> None:
    client = client or SecClient()
    n = refresh_tickers(conn, client)
    log.info("tickers: %d", n)
    issuers = active_issuers(conn, days)
    log.info("active issuers (last %d days): %d", days, len(issuers))
    refresh_shares(conn, client, issuers, force=force)
    n_y, n_f = refresh_prices(conn, issuers)
    log.info("prices: %d from yahoo, %d from latest Form 4 price", n_y, n_f)
