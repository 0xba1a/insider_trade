"""Command line interface: `insider <command>`."""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import tempfile
from datetime import date, timedelta

import requests
from rich.console import Console
from rich.table import Table

from . import config, db
from .fmt import money as _money, num as _num, pct as _pct
from .rank import RankParams, rank, ticker_detail

console = Console()
DEFAULT_REPO = os.environ.get("INSIDER_REPO", "0xba1a/insider_trade")
DATA_BRANCH = "data"


def _date(s: str) -> date:
    return date.fromisoformat(s)


# ---------------------------------------------------------------- commands

def cmd_ingest(args) -> None:
    from .ingest import default_start, ingest_range, today_et

    conn = db.connect()
    start = args.start or default_start(conn)
    end = args.end or (today_et() - timedelta(days=1))
    console.print(f"Ingesting EDGAR daily index {start} → {end}")
    ingest_range(conn, start, end, force=args.force)


def cmd_backfill(args) -> None:
    from .dataset import backfill
    from .ingest import ingest_range, today_et

    conn = db.connect()
    end = today_et() - timedelta(days=1)
    start = end - timedelta(days=args.days)
    console.print(f"Backfilling {start} → {end} from quarterly data sets")
    next_day = backfill(conn, start, end, force=args.force)
    if not args.datasets_only and next_day <= end:
        console.print(f"Filling {next_day} → {end} from the EDGAR daily index")
        ingest_range(conn, next_day, end)


def cmd_market(args) -> None:
    from .market import refresh_market

    refresh_market(db.connect(), days=args.days, force=args.force)


def _params(args) -> RankParams:
    return RankParams(
        window_days=args.window, as_of=args.as_of,
        w_cost=args.w_cost, w_recurrence=args.w_recurrence, w_insiders=args.w_insiders,
        min_value=args.min_value, min_insiders=args.min_insiders,
        include_funds=args.include_funds, include_unlisted=args.include_unlisted,
    )


def cmd_rank(args) -> None:
    p = _params(args)
    df = rank(db.connect(), p)
    if df.empty:
        console.print("[yellow]No purchases in the selected window.[/]")
        return
    top = df.head(args.top)
    t = Table(
        title=f"Insider buying rank — {p.window_days}d window ending {p.as_of or date.today()} "
        f"({len(df)} stocks; weights cost {p.w_cost}/recur {p.w_recurrence}/insiders {p.w_insiders})",
        title_justify="left",
    )
    cols = [
        ("#", "right"), ("Ticker", "left"), ("Company", "left"), ("Score", "right"),
        ("$ Bought", "right"), ("% MCap", "right"), ("ΔOwn", "right"), ("Ins", "right"),
        ("Roles", "left"), ("Days", "right"), ("Rpt", "right"), ("Last buy", "left"),
        ("Avg buy", "right"), ("Price", "right"), ("Chg", "right"),
    ]
    if args.details:
        cols[4:4] = [("Cost", "right"), ("Recur", "right"), ("InsS", "right")]
    for name, j in cols:
        t.add_column(name, justify=j, no_wrap=name not in ("Company", "Roles"))
    for i, r in top.iterrows():
        row = [
            str(i + 1), r.ticker + ("*" if r.price_mismatch else ""), str(r.company)[:28], f"{r.score:.1f}",
            _money(r.total_value), _pct(r.pct_mcap, 3), _pct(r.delta_own, 0), str(int(r.n_insiders)),
            r.roles, str(int(r.buy_days)), str(int(r.repeat_buyers)), r.last_buy,
            _num(r.avg_buy_price), _num(r.price), _pct(r.price_chg, 0),
        ]
        if args.details:
            row[4:4] = [f"{r.s_cost:.0f}", f"{r.s_recurrence:.0f}", f"{r.s_insiders:.0f}"]
        t.add_row(*row)
    console.print(t)
    console.print(
        "[dim]ΔOwn = avg increase in each insider's holdings (capped at 100%). Ins = distinct insiders. "
        "Days = distinct buy dates. Rpt = insiders buying on 2+ dates. Chg = price vs avg buy.\n"
        "* = filing prices don't match the market quote (foreign currency / ADR): $ excluded from cost score.[/]"
    )


def cmd_show(args) -> None:
    p = _params(args)
    df = ticker_detail(db.connect(), args.ticker, p)
    if df.empty:
        console.print(f"[yellow]No purchases for {args.ticker.upper()} in the last {p.window_days} days.[/]")
        return
    t = Table(title=f"{args.ticker.upper()} — {df.issuer_name.iloc[0]} — purchases, last {p.window_days} days", title_justify="left")
    for name, j in [("Trade", "left"), ("Filed", "left"), ("Insider", "left"), ("Role", "left"), ("Title", "left"),
                    ("Shares", "right"), ("Price", "right"), ("Value", "right"), ("Owned after", "right"),
                    ("D/I", "left"), ("Form", "left"), ("Accession", "left")]:
        t.add_column(name, justify=j)
    for r in df.itertuples():
        t.add_row(
            r.trans_date, r.filing_date, str(r.owner_name), str(r.owner_role), str(r.owner_title or "")[:30],
            _num(r.shares, "{:,.0f}"), _num(r.price), _money(r.value), _num(r.shares_after, "{:,.0f}"),
            str(r.direct_indirect or ""), r.form_type, r.accession,
        )
    console.print(t)
    console.print(f"Total: {_money(df.value.sum())} across {df.owner_cik.nunique()} insiders")


def cmd_status(args) -> None:
    conn = db.connect()
    q = lambda sql: conn.execute(sql).fetchone()  # noqa: E731
    n_f, n_p = q("SELECT (SELECT COUNT(*) FROM filings), (SELECT COUNT(*) FROM purchases)")
    lo, hi = q("SELECT MIN(trans_date), MAX(trans_date) FROM purchases WHERE trans_date >= '2000'")
    last = q("SELECT MAX(index_date) FROM ingest_log WHERE status IN ('done','dataset')")[0]
    quarters = [r[0] for r in conn.execute("SELECT quarter FROM dataset_log ORDER BY quarter")]
    md = q("SELECT COUNT(*), SUM(shares_outstanding IS NOT NULL), SUM(price_source='yahoo'), MAX(price_asof) FROM market_data")
    console.print(f"Database:          {config.db_path()}")
    console.print(f"Filings / rows:    {n_f:,} filings with purchases, {n_p:,} purchase rows")
    console.print(f"Trade dates:       {lo} → {hi}")
    console.print(f"Ingested through:  {last} (filing date)")
    console.print(f"Quarterly sets:    {', '.join(quarters) or '-'}")
    console.print(f"Market data:       {md[0] or 0} issuers, {md[1] or 0} with shares outstanding, "
                  f"{md[2] or 0} Yahoo prices (as of {md[3]})")


def _site_url(repo: str) -> str:
    owner, name = repo.split("/", 1)
    return f"https://{owner.lower()}.github.io/{name}/"


def cmd_report(args) -> None:
    from pathlib import Path

    from .report import data_asof, render_readme, site_payload, take_snapshot, update_readme, write_site

    conn = db.connect()
    as_of = args.as_of or data_asof(conn)
    if as_of is None:
        raise SystemExit("No ingested data yet.")
    ranked, purchases = take_snapshot(conn, as_of, top_n=args.top)
    console.print(f"Snapshot {as_of}: {len(ranked)} stocks ranked")
    site_url = args.site_url or _site_url(args.repo)
    if args.readme:
        changed = update_readme(Path(args.readme), render_readme(conn, as_of, ranked, site_url, args.top))
        console.print(f"{args.readme}: {'updated' if changed else 'unchanged'}")
    if args.site:
        write_site(Path(args.site), site_payload(conn, as_of, ranked, purchases, args.repo, args.top), ranked)
        console.print(f"Site written to {args.site}/")


def cmd_snapshots(args) -> None:
    from .report import backfill_snapshots, data_asof

    conn = db.connect()
    until = args.until or data_asof(conn)
    n = backfill_snapshots(conn, args.since, until, force=args.force)
    console.print(f"{n} snapshots computed ({args.since} → {until})")


def cmd_sync(args) -> None:
    url = f"https://raw.githubusercontent.com/{args.repo}/{DATA_BRANCH}/data/insider.db"
    dest = config.db_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    console.print(f"Downloading {url}")
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as tmp:
            shutil.copyfileobj(r.raw, tmp)
    os.replace(tmp.name, dest)
    console.print(f"Saved to {dest}")
    cmd_status(args)


# ---------------------------------------------------------------- parser

def _add_rank_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--window", type=int, default=90, help="lookback window in days on trade date (default 90)")
    p.add_argument("--as-of", type=_date, default=None, help="end of window, YYYY-MM-DD (default today)")
    p.add_argument("--w-cost", type=float, default=0.4, help="weight for cost factor (default 0.4)")
    p.add_argument("--w-recurrence", type=float, default=0.3, help="weight for recurrence factor (default 0.3)")
    p.add_argument("--w-insiders", type=float, default=0.3, help="weight for insider-count factor (default 0.3)")
    p.add_argument("--min-value", type=float, default=0, help="minimum total $ bought per stock")
    p.add_argument("--min-insiders", type=int, default=1, help="minimum distinct insiders per stock")
    p.add_argument("--include-funds", action="store_true", help="include 10%% owners / other non-executive buyers")
    p.add_argument("--include-unlisted", action="store_true", help="include issuers without a ticker")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="insider", description="Rank US stocks by SEC Form 4 insider purchases.")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="ingest Form 4 filings from the EDGAR daily index")
    p.add_argument("--start", type=_date, help="first filing date (default: day after last ingested)")
    p.add_argument("--end", type=_date, help="last filing date (default: yesterday, US/Eastern)")
    p.add_argument("--force", action="store_true", help="re-ingest dates already processed")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("backfill", help="load history from SEC quarterly data sets + daily index")
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--force", action="store_true", help="reload quarters already loaded")
    p.add_argument("--datasets-only", action="store_true", help="skip daily-index fill after the last quarter")
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("market", help="refresh tickers, shares outstanding and prices")
    p.add_argument("--days", type=int, default=400, help="issuers with purchases in this many days")
    p.add_argument("--force", action="store_true", help="refetch shares outstanding even if fresh")
    p.set_defaults(func=cmd_market)

    p = sub.add_parser("rank", help="rank stocks by insider buying")
    _add_rank_args(p)
    p.add_argument("--top", type=int, default=25)
    p.add_argument("--details", action="store_true", help="show factor sub-scores")
    p.set_defaults(func=cmd_rank)

    p = sub.add_parser("show", help="list the purchases behind a ticker's ranking")
    p.add_argument("ticker")
    _add_rank_args(p)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("status", help="database summary")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("report", help="snapshot today's ranking; write the README section and static site")
    p.add_argument("--as-of", type=_date, default=None, help="snapshot date (default: last ingested filing date)")
    p.add_argument("--top", type=int, default=30, help="stocks in the README table / tracked for changes")
    p.add_argument("--readme", help="README to update between the insider-report markers")
    p.add_argument("--site", help="directory to write the static site to")
    p.add_argument("--repo", default=DEFAULT_REPO, help=f"owner/name (default {DEFAULT_REPO})")
    p.add_argument("--site-url", help="published site URL (default: GitHub Pages URL of --repo)")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("snapshots", help="compute historical daily ranking snapshots and change events")
    p.add_argument("--since", type=_date, required=True)
    p.add_argument("--until", type=_date, help="default: last ingested filing date")
    p.add_argument("--force", action="store_true", help="recompute existing snapshots")
    p.set_defaults(func=cmd_snapshots)

    p = sub.add_parser("sync", help="download the latest database from the GitHub data branch")
    p.add_argument("--repo", default=DEFAULT_REPO, help=f"owner/name (default {DEFAULT_REPO})")
    p.set_defaults(func=cmd_sync)
    return ap


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for noisy in ("urllib3", "peewee"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # yfinance logs an ERROR per delisted/unknown symbol; those fall back to Form 4 prices.
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    try:
        args.func(args)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
