"""Aggregate insider purchases per issuer and score them."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

EXEC_ROLES = ("CEO", "CFO", "Officer", "Dir")
ROLE_ORDER = ["CEO", "CFO", "Officer", "Dir", "10%", "Other"]
DELTA_OWN_CAP = 1.0  # a new position or doubling counts as 100% for scoring
PRICE_RATIO_BAND = (0.67, 1.5)
PRICE_OUTLIER_FACTOR = 50.0


@dataclass
class RankParams:
    window_days: int = 90
    as_of: date | None = None
    w_cost: float = 0.4
    w_recurrence: float = 0.3
    w_insiders: float = 0.3
    min_value: float = 0.0
    min_insiders: int = 1
    include_funds: bool = False
    include_unlisted: bool = False


def load_purchases(conn: sqlite3.Connection, start: date, end: date) -> pd.DataFrame:
    q = """
        SELECT p.accession, p.seq, p.trans_date, p.shares, p.price, p.value, p.shares_after,
               p.direct_indirect, p.security_title,
               f.form_type, f.filing_date, f.issuer_cik, f.issuer_name,
               COALESCE(t.ticker, NULLIF(f.issuer_symbol, '')) AS ticker,
               f.owner_cik, f.owner_name, f.owner_title, f.owner_role, f.owner_weight
        FROM purchases p
        JOIN filings f USING (accession)
        LEFT JOIN tickers t ON t.cik = f.issuer_cik
        WHERE p.trans_date BETWEEN ? AND ? AND f.filing_date <= ? AND p.shares > 0 AND p.price > 0
    """
    return pd.read_sql_query(q, conn, params=(start.isoformat(), end.isoformat(), end.isoformat()))


def apply_amendments(df: pd.DataFrame) -> pd.DataFrame:
    """A 4/A restates an earlier filing: for each (issuer, owner, trade date) that has an
    amendment, keep only the rows from the most recent amendment."""
    if df.empty:
        return df
    key = ["issuer_cik", "owner_cik", "trans_date"]
    amend = df[df.form_type == "4/A"]
    if amend.empty:
        return df
    latest = (
        amend.sort_values(["filing_date", "accession"])
        .groupby(key, as_index=False)["accession"].last()
        .rename(columns={"accession": "_keep_acc"})
    )
    m = df.merge(latest, on=key, how="left")
    m = m[m._keep_acc.isna() | (m.accession == m._keep_acc)]
    return m.drop(columns="_keep_acc")


def dedupe_related_filers(df: pd.DataFrame) -> pd.DataFrame:
    """Related owners (e.g. a fund and its managing director) sometimes report the same trade in
    separate filings. Identical issuer/date/shares/price/holdings-after rows from different
    accessions are the same trade: keep the row of the most senior filer."""
    if df.empty:
        return df
    key = ["issuer_cik", "trans_date", "shares", "price", "shares_after"]
    d = df.sort_values(["owner_weight", "accession", "seq"], ascending=[False, True, True])
    first_acc = d.groupby(key, dropna=False)["accession"].transform("first")
    return d[d.accession == first_acc].sort_index()


def drop_price_outliers(df: pd.DataFrame, factor: float = PRICE_OUTLIER_FACTOR) -> pd.DataFrame:
    """Drop rows priced `factor`× above the issuer's median purchase price (needs 3+ rows).
    Catches decimal slips that inflate the $ total, e.g. 614.00 reported for 6.14. Low prices are
    kept: they are usually real (ordinary shares vs ADRs, pre-split prices)."""
    if df.empty:
        return df
    g = df.groupby("issuer_cik").price
    med, n = g.transform("median"), g.transform("size")
    return df[(n < 3) | (df.price <= med * factor)]


def filter_purchases(df: pd.DataFrame, p: RankParams) -> pd.DataFrame:
    df = dedupe_related_filers(apply_amendments(df))
    if not p.include_funds:
        df = df[df.owner_role.isin(EXEC_ROLES)]
    if not p.include_unlisted:
        df = df[df.ticker.notna() & (df.ticker != "")]
    return drop_price_outliers(df)


def insider_delta_own(df: pd.DataFrame) -> pd.DataFrame:
    """Per (issuer, insider): shares bought in window relative to holdings before the first buy."""
    if df.empty:
        return pd.DataFrame(columns=["issuer_cik", "owner_cik", "delta_own"])
    d = df.sort_values(["trans_date", "accession", "seq"])
    lines = d.groupby(["issuer_cik", "owner_cik", "direct_indirect"], dropna=False).agg(
        first_shares=("shares", "first"), first_after=("shares_after", "first"), bought=("shares", "sum")
    ).reset_index()
    lines["prior"] = (lines.first_after - lines.first_shares).clip(lower=0)
    per = lines.groupby(["issuer_cik", "owner_cik"]).agg(
        prior=("prior", lambda s: s.sum(min_count=1)), bought=("bought", "sum")
    ).reset_index()
    per["delta_own"] = np.where(
        per.prior.isna(), np.nan, np.where(per.prior > 0, per.bought / per.prior.where(per.prior > 0), np.inf)
    )
    return per[["issuer_cik", "owner_cik", "delta_own"]]


def _roles_summary(roles: pd.Series) -> str:
    counts = roles.value_counts()
    parts = []
    for r in ROLE_ORDER:
        if r in counts:
            parts.append(r if counts[r] == 1 else f"{counts[r]} {r}")
    return ", ".join(parts)


def aggregate(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    per_insider = df.sort_values("owner_weight", ascending=False).groupby(["issuer_cik", "owner_cik"]).agg(
        weight=("owner_weight", "max"), role=("owner_role", "first"), days=("trans_date", "nunique")
    ).reset_index()
    per_insider = per_insider.merge(insider_delta_own(df), on=["issuer_cik", "owner_cik"], how="left")
    per_insider["delta_own_capped"] = per_insider.delta_own.clip(upper=DELTA_OWN_CAP)

    g = df.groupby("issuer_cik")
    agg = g.agg(
        ticker=("ticker", "first"), company=("issuer_name", "last"),
        total_value=("value", "sum"), total_shares=("shares", "sum"),
        n_tx=("value", "size"), buy_days=("trans_date", "nunique"),
        first_buy=("trans_date", "min"), last_buy=("trans_date", "max"),
    )
    ig = per_insider.groupby("issuer_cik")
    agg["n_insiders"] = ig.size()
    agg["weighted_insiders"] = ig.weight.sum()
    agg["repeat_buyers"] = ig.days.apply(lambda s: int((s >= 2).sum()))
    agg["delta_own"] = ig.delta_own_capped.mean()
    agg["roles"] = ig.role.apply(_roles_summary)
    adj_shares = (df.shares * df.split_factor) if "split_factor" in df else df.shares
    agg["avg_buy_price"] = agg.total_value / adj_shares.groupby(df.issuer_cik).sum()  # in today's share terms
    return agg.reset_index()


def _pct_rank(s: pd.Series) -> pd.Series:
    return s.rank(pct=True, method="average")


def score(agg: pd.DataFrame, market: pd.DataFrame, p: RankParams) -> pd.DataFrame:
    if agg.empty:
        return agg
    a = agg.merge(market, left_on="issuer_cik", right_on="cik", how="left")
    # Filing prices not comparable with the quote (foreign currency, ADR ratio):
    # market-cap and price-change figures would be meaningless.
    ratio = a.price_ratio
    a["price_mismatch"] = ratio.notna() & ((ratio < PRICE_RATIO_BAND[0]) | (ratio > PRICE_RATIO_BAND[1]))
    a["market_cap"] = a.shares_outstanding * a.price
    a["pct_mcap"] = (a.total_value / a.market_cap).where(~a.price_mismatch)
    a.loc[a.pct_mcap > 1, "pct_mcap"] = np.nan  # >100% of market cap means stale share counts
    a["price_chg"] = np.where((a.price_source == "yahoo") & ~a.price_mismatch, a.price / a.avg_buy_price - 1, np.nan)

    a = a[(a.total_value >= p.min_value) & (a.n_insiders >= p.min_insiders)].copy()
    if a.empty:
        return a

    cost = pd.concat(
        [_pct_rank(a.total_value.where(~a.price_mismatch)), _pct_rank(a.pct_mcap), _pct_rank(a.delta_own)], axis=1
    ).mean(axis=1, skipna=True)
    recurrence = _pct_rank(a.buy_days + a.repeat_buyers)
    insiders = _pct_rank(a.weighted_insiders)
    wsum = p.w_cost + p.w_recurrence + p.w_insiders
    a["s_cost"] = cost * 100
    a["s_recurrence"] = recurrence * 100
    a["s_insiders"] = insiders * 100
    a["score"] = (p.w_cost * cost + p.w_recurrence * recurrence + p.w_insiders * insiders) / wsum * 100
    return a.sort_values(["score", "total_value"], ascending=False).reset_index(drop=True)


def load_market(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT cik, shares_outstanding, price, price_asof, price_source, price_ratio FROM market_data", conn
    )


def window(p: RankParams) -> tuple[date, date]:
    end = p.as_of or date.today()
    return end - timedelta(days=p.window_days), end


def load_splits(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query("SELECT ticker, date, ratio FROM splits", conn)


def apply_splits(df: pd.DataFrame, splits: pd.DataFrame) -> pd.DataFrame:
    """Add `split_factor`: product of splits after each trade, to restate shares in today's terms."""
    df = df.copy()
    df["split_factor"] = 1.0
    if df.empty or splits.empty:
        return df
    for t, g in splits.groupby("ticker"):
        rows = df.ticker == t
        if not rows.any():
            continue
        for d, r in zip(g.date, g.ratio):
            df.loc[rows & (df.trans_date < d), "split_factor"] *= r
    return df


def rank_with_purchases(conn: sqlite3.Connection, p: RankParams) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The ranking and the filtered purchase rows behind it."""
    start, end = window(p)
    df = apply_splits(filter_purchases(load_purchases(conn, start, end), p), load_splits(conn))
    return score(aggregate(df), load_market(conn), p), df


def rank(conn: sqlite3.Connection, p: RankParams) -> pd.DataFrame:
    return rank_with_purchases(conn, p)[0]


def ticker_detail(conn: sqlite3.Connection, ticker: str, p: RankParams) -> pd.DataFrame:
    start, end = window(p)
    df = filter_purchases(load_purchases(conn, start, end), p)
    df = df[df.ticker.str.upper() == ticker.upper()]
    if df.empty:
        return df
    per = insider_delta_own(df)
    df = df.merge(per, on=["issuer_cik", "owner_cik"], how="left")
    return df.sort_values(["trans_date", "accession", "seq"], ascending=[False, True, True])
