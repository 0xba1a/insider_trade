"""Tickers, shares outstanding (SEC XBRL) and prices (yfinance) for market-cap normalization."""
from __future__ import annotations

import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

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
MAX_SHARES_AGE_DAYS = 548
FRESH_SHARES_DAYS = 200
PRICE_HISTORY = "1y"


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
    """Most recent shares-outstanding value across the concept fallbacks. A concept may be
    stale (e.g. dei last tagged years ago), so keep looking until one is reasonably fresh."""
    today = date.today()
    cutoff = (today - timedelta(days=MAX_SHARES_AGE_DAYS)).isoformat()
    fresh = (today - timedelta(days=FRESH_SHARES_DAYS)).isoformat()
    best: tuple[float, str] | None = None
    for concept, sum_classes in SHARE_CONCEPTS:
        try:
            data = client.get_json(CONCEPT_URL.format(cik=cik, concept=concept))
        except NotFound:
            continue
        res = latest_shares(data.get("units", {}).get("shares", []), sum_classes)
        if res and res[0] > 0 and res[1] >= cutoff and (best is None or res[1] > best[1]):
            best = res
        if best and best[1] >= fresh:
            break
    return best


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


def fetch_closes(tickers: list[str], period: str = PRICE_HISTORY, chunk: int = 200) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Daily split-adjusted closes and split events (columns = our tickers) from yfinance."""
    import yfinance as yf  # imported lazily; slow import

    closes, splits = [], []
    for i in range(0, len(tickers), chunk):
        batch = tickers[i : i + chunk]
        symbols = [_yahoo_symbol(t) for t in batch]
        try:
            df = yf.download(symbols, period=period, progress=False, auto_adjust=False, actions=True, threads=True)
        except Exception as e:
            log.warning("yfinance batch failed: %s", e)
            continue
        if df is None or df.empty:
            continue
        for field, out in (("Close", closes), ("Stock Splits", splits)):
            if field not in df.columns.get_level_values(0):
                continue
            f = df[field]
            if not hasattr(f, "columns"):  # single symbol returns a Series
                f = f.to_frame(symbols[0])
            f = f.rename(columns=dict(zip(symbols, batch)))
            out.append(f[[t for t in batch if t in f.columns]])

    def combine(frames):
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames, axis=1).sort_index()
        out.index = pd.to_datetime(out.index).tz_localize(None)
        return out

    return combine(closes), combine(splits)


def latest_closes(closes: pd.DataFrame) -> dict[str, tuple[float, str]]:
    out = {}
    for t in closes.columns:
        s = closes[t].dropna()
        s = s[s > 0]
        if not s.empty:
            out[t] = (float(s.iloc[-1]), str(s.index[-1].date()))
    return out


def _close_on(s: pd.Series, dates: np.ndarray) -> np.ndarray:
    pos = s.index.searchsorted(dates, side="right") - 1  # last close on or before the date
    return np.where(pos >= 0, s.to_numpy()[np.clip(pos, 0, None)], np.nan)


def split_events(splits: pd.DataFrame) -> list[tuple[str, str, float]]:
    """(ticker, date, ratio) rows from the yfinance 'Stock Splits' frame."""
    out = []
    for t in splits.columns:
        s = splits[t].dropna()
        for d, r in s[s > 0].items():
            if r != 1.0:
                out.append((t, str(pd.Timestamp(d).date()), float(r)))
    return out


def price_ratios(purchases: pd.DataFrame, closes: pd.DataFrame, splits: pd.DataFrame | None = None) -> dict[str, float]:
    """Median of (Form 4 price / close on the trade date) per ticker. Yahoo closes are
    split-adjusted, so they are first restated in the share terms of the trade date. A ratio far
    from 1 then means the filing's prices aren't in the quote's terms (local currency, ordinary
    shares vs ADR)."""
    out = {}
    for t, g in purchases.groupby("ticker"):
        if t not in closes.columns:
            continue
        s = closes[t].dropna()
        s = s[s > 0]
        if s.empty:
            continue
        if splits is not None and t in splits.columns:
            sp = splits[t].reindex(s.index).fillna(0.0).replace(0.0, 1.0)
            if (sp != 1.0).any():
                after = sp[::-1].cumprod()[::-1] / sp  # product of splits strictly after each date
                s = s * after
        ratios = g.price.to_numpy(dtype=float) / _close_on(s, pd.to_datetime(g.trans_date).to_numpy())
        ratios = ratios[np.isfinite(ratios)]
        if len(ratios):
            out[t] = float(np.median(ratios))
    return out


def refresh_prices(conn: sqlite3.Connection, issuers: list[sqlite3.Row]) -> tuple[int, int]:
    """Update prices from yfinance; fall back to the latest Form 4 purchase price.
    Returns (n_yahoo, n_fallback)."""
    tickers = sorted({r["ticker"] for r in issuers})
    closes, splits = fetch_closes(tickers) if tickers else (pd.DataFrame(), pd.DataFrame())
    prices = latest_closes(closes)
    since = (date.today() - timedelta(days=365)).isoformat()
    purchases = pd.read_sql_query(
        """
        SELECT f.issuer_cik AS cik, COALESCE(t.ticker, f.issuer_symbol) AS ticker, p.trans_date, p.price
        FROM purchases p JOIN filings f USING (accession) LEFT JOIN tickers t ON t.cik = f.issuer_cik
        WHERE p.price > 0 AND p.shares > 0 AND p.trans_date >= ?
        ORDER BY p.trans_date
        """,
        conn, params=(since,),
    )
    ratios = price_ratios(purchases, closes, splits) if not closes.empty else {}
    if not splits.empty:
        conn.executemany(
            "INSERT OR REPLACE INTO splits (ticker, date, ratio) VALUES (?,?,?)", split_events(splits)
        )
    fallback = {r.cik: (r.price, r.trans_date) for r in purchases.itertuples()}  # latest wins
    n_y = n_f = 0
    for r in issuers:
        ratio = None
        if r["ticker"] in prices:
            price, asof = prices[r["ticker"]]
            source = "yahoo"
            ratio = ratios.get(r["ticker"])
            n_y += 1
        elif r["cik"] in fallback:
            price, asof = fallback[r["cik"]]
            source = "form4"
            n_f += 1
        else:
            continue
        conn.execute(
            """INSERT INTO market_data (cik, ticker, price, price_asof, price_source, price_ratio)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(cik) DO UPDATE SET ticker=excluded.ticker, price=excluded.price,
                 price_asof=excluded.price_asof, price_source=excluded.price_source,
                 price_ratio=excluded.price_ratio""",
            (r["cik"], r["ticker"], price, asof, source, ratio),
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
