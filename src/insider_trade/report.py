"""Daily ranking snapshots, change detection, and the published outputs (README section + static site)."""
from __future__ import annotations

import json
import math
import re
import shutil
import sqlite3
from datetime import date, datetime, timedelta, timezone
from importlib import resources
from pathlib import Path

import numpy as np
import pandas as pd

from .fmt import money, pct
from .rank import RankParams, _roles_summary, rank_with_purchases

TOP_N = 30
BIG_MOVE = 5            # places moved within the top N to count as a change
NEW_ENTRY_LOOKBACK = 10  # a new entry must have been outside the top N in this many previous snapshots
HISTORY_DAYS = 365      # change log kept on the site
README_RECENT_DAYS = 7  # change log shown in the README
README_MAX_EVENTS = 50
START_MARK = "<!-- insider-report:start -->"
END_MARK = "<!-- insider-report:end -->"
EVENT_ORDER = {"first_time": 0, "new_entry": 1, "big_move": 2, "new_buys": 3}


def data_asof(conn: sqlite3.Connection) -> date | None:
    """Last filing date that has been ingested."""
    v = conn.execute("SELECT MAX(index_date) FROM ingest_log WHERE status IN ('done','dataset')").fetchone()[0]
    return date.fromisoformat(v) if v else None


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def detect_events(
    as_of: str, ranked: pd.DataFrame, purchases: pd.DataFrame, prev_as_of: str, prev_rank: dict[int, int],
    recent_top: set[int] | None = None, top_n: int = TOP_N, big_move: int = BIG_MOVE,
) -> list[dict]:
    """Compare a ranking with the previous snapshot.

    first_time: stock wasn't ranked at all before (no insider buying in the previous window).
    new_entry:  stock entered the top N and wasn't in it in the recent snapshots (`recent_top`),
                so stocks hovering around the cut-off don't re-enter every other day.
    big_move:   moved `big_move`+ places while staying in the top N.
    new_buys:   top-N stock with purchases filed since the previous snapshot."""
    recent_top = recent_top if recent_top is not None else {c for c, r in prev_rank.items() if r <= top_n}
    fresh = purchases[purchases.filing_date > prev_as_of]
    buys = {}
    for cik, g in fresh.groupby("issuer_cik"):
        owners = g.drop_duplicates("owner_cik")
        buys[int(cik)] = (float(g.value.sum()), len(owners), _roles_summary(owners.owner_role))

    events = []
    for r in ranked.itertuples():
        cik, rk = int(r.issuer_cik), int(r.rank)
        pr = prev_rank.get(cik)

        def add(event, value=None, detail=None):
            events.append(dict(
                as_of=as_of, cik=cik, event=event, ticker=r.ticker, company=r.company,
                rank=rk, prev_rank=pr, value=value, detail=detail,
            ))

        if pr is None:
            add("first_time", float(r.total_value), f"{_plural(int(r.n_insiders), 'insider')}: {r.roles}")
        if rk <= top_n:
            if cik not in recent_top and (pr is None or pr > top_n):
                add("new_entry")
            elif pr is not None and pr <= top_n and abs(pr - rk) >= big_move:
                add("big_move")
            if cik in buys:
                v, n, roles = buys[cik]
                add("new_buys", v, f"{_plural(n, 'insider')}: {roles}")
    return events


def take_snapshot(
    conn: sqlite3.Connection, as_of: date, top_n: int = TOP_N, big_move: int = BIG_MOVE
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Rank as of `as_of` (default parameters), store the snapshot and its change events.
    Returns (ranking with `rank`/`prev_rank` columns, purchases behind it)."""
    ranked, purchases = rank_with_purchases(conn, RankParams(as_of=as_of))
    ranked = ranked.copy()
    ranked["rank"] = np.arange(1, len(ranked) + 1)
    key = as_of.isoformat()
    prev = conn.execute("SELECT MAX(as_of) FROM rank_snapshots WHERE as_of < ?", (key,)).fetchone()[0]
    prev_rank = (
        {int(r[0]): int(r[1]) for r in conn.execute("SELECT cik, rank FROM rank_snapshots WHERE as_of = ?", (prev,))}
        if prev else {}
    )
    recent_top = {int(r[0]) for r in conn.execute(
        """SELECT DISTINCT cik FROM rank_snapshots WHERE rank <= ? AND as_of IN (
               SELECT DISTINCT as_of FROM rank_snapshots WHERE as_of < ? ORDER BY as_of DESC LIMIT ?)""",
        (top_n, key, NEW_ENTRY_LOOKBACK),
    )}
    events = detect_events(key, ranked, purchases, prev, prev_rank, recent_top, top_n, big_move) if prev else []
    with conn:
        conn.execute("DELETE FROM rank_snapshots WHERE as_of = ?", (key,))
        conn.executemany(
            "INSERT INTO rank_snapshots (as_of, rank, cik, ticker, score, total_value) VALUES (?,?,?,?,?,?)",
            [(key, int(r.rank), int(r.issuer_cik), r.ticker, float(r.score), float(r.total_value))
             for r in ranked.itertuples()],
        )
        conn.execute("DELETE FROM rank_events WHERE as_of = ?", (key,))
        conn.executemany(
            """INSERT INTO rank_events (as_of, cik, event, ticker, company, rank, prev_rank, value, detail)
               VALUES (:as_of, :cik, :event, :ticker, :company, :rank, :prev_rank, :value, :detail)""",
            events,
        )
    ranked["prev_rank"] = ranked.issuer_cik.map(prev_rank) if prev else np.nan
    return ranked, purchases


def backfill_snapshots(conn: sqlite3.Connection, since: date, until: date, force: bool = False) -> int:
    """Snapshot every filing date in [since, until]. Snapshots after the first newly added date are
    recomputed so their change events stay relative to the right predecessor."""
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT filing_date FROM filings WHERE filing_date BETWEEN ? AND ? ORDER BY 1",
        (since.isoformat(), until.isoformat()),
    )]
    existing = {r[0] for r in conn.execute("SELECT DISTINCT as_of FROM rank_snapshots")}
    missing = [d for d in dates if d not in existing]
    if force:
        todo = dates
    elif missing:
        todo = [d for d in dates if d >= missing[0]]
    else:
        todo = []
    for d in todo:
        take_snapshot(conn, date.fromisoformat(d))
    return len(todo)


def load_events(conn: sqlite3.Connection, since: date) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT * FROM rank_events WHERE as_of >= ? ORDER BY as_of DESC, rank", conn, params=(since.isoformat(),)
    )


# ---------------------------------------------------------------- README

def _md(s) -> str:
    return str(s if s is not None else "").replace("|", "\\|").replace("\n", " ").strip()


def _arrow(prev: float | None, rk: int, top_n: int) -> str:
    if prev is None or (isinstance(prev, float) and math.isnan(prev)):
        return "🆕"
    prev = int(prev)
    if prev > top_n:
        return f"⬆️ #{prev}"
    if prev > rk:
        return f"▲{prev - rk}"
    if prev < rk:
        return f"▼{rk - prev}"
    return "–"


def _event_text(e: dict, top_n: int) -> str:
    kind = e["event"]
    if kind == "first_time":
        return f"🆕 First insider buying: {money(e['value'])} · {e['detail']}"
    if kind == "new_entry":
        return f"⬆️ Entered the top {top_n} (was #{int(e['prev_rank'])})"
    if kind == "big_move":
        prev, rk = int(e["prev_rank"]), int(e["rank"])
        arrow = "▲" if prev > rk else "▼"
        return f"{arrow} {abs(prev - rk)} places (#{prev} → #{rk})"
    if kind == "new_buys":
        return f"💵 New buys filed: {money(e['value'])} · {e['detail']}"
    return kind


def _changes_rows(events: pd.DataFrame, top_n: int) -> list[dict]:
    """One row per (date, stock) combining its events, restricted to the top N."""
    rows = []
    ev = events[events["rank"] <= top_n]
    for (as_of, cik), g in ev.groupby(["as_of", "cik"], sort=False):
        g = g.sort_values("event", key=lambda s: s.map(EVENT_ORDER))
        kinds = set(g.event)
        recs = [r for r in g.to_dict("records") if not (r["event"] == "new_entry" and "first_time" in kinds)]
        first = g.iloc[0]
        rows.append(dict(
            as_of=as_of, ticker=first.ticker, company=first.company, rank=int(first["rank"]),
            text="<br>".join(_event_text(r, top_n) for r in recs), order=min(EVENT_ORDER[k] for k in kinds),
        ))
    rows.sort(key=lambda r: (r["as_of"], -r["order"], -r["rank"]), reverse=True)
    return rows


def render_readme(
    conn: sqlite3.Connection, as_of: date, ranked: pd.DataFrame, site_url: str, top_n: int = TOP_N
) -> str:
    price_asof = conn.execute("SELECT MAX(price_asof) FROM market_data WHERE price_source = 'yahoo'").fetchone()[0]
    p = RankParams()
    recent = load_events(conn, as_of - timedelta(days=README_RECENT_DAYS - 1))
    today = recent[recent.as_of == as_of.isoformat()]
    today_new_buys = {int(c) for c in today[today.event == "new_buys"].cik}
    link = lambda t: f"[{_md(t)}]({site_url}#{t})"  # noqa: E731

    out = [
        START_MARK,
        "<!-- Generated by `insider report`; edits inside this block are overwritten daily. -->",
        "## Latest ranking",
        "",
        f"**SEC filings through {as_of}** · prices as of {price_asof or '-'} · {p.window_days}-day window · "
        f"{len(ranked):,} stocks with insider buying",
        "",
        f"👉 **[Full sortable ranking, purchase details and the 1-year change log]({site_url})**",
        "",
        f"### What changed (last {README_RECENT_DAYS} days)",
        "",
    ]
    changes = _changes_rows(recent, top_n)
    if changes:
        out += ["| Filed through | Ticker | Company | Rank | Change |", "|---|---|---|--:|---|"]
        for c in changes[:README_MAX_EVENTS]:
            out.append(f"| {c['as_of']} | {link(c['ticker'])} | {_md(c['company'])[:32]} | {c['rank']} | {c['text']} |")
        if len(changes) > README_MAX_EVENTS:
            out.append(f"\n…and {len(changes) - README_MAX_EVENTS} more on the [site]({site_url}).")
    else:
        out.append(f"_No changes in the top {top_n}._")

    out += [
        "",
        f"### Top {top_n}",
        "",
        "| # | Δ | Ticker | Company | Score | $ Bought | % MCap | ΔOwn | Insiders | Roles | Buy days | Last buy | Chg |",
        "|--:|:-:|---|---|--:|--:|--:|--:|--:|---|--:|---|--:|",
    ]
    for r in ranked.head(top_n).itertuples():
        delta = _arrow(r.prev_rank, int(r.rank), top_n) + (" 💵" if int(r.issuer_cik) in today_new_buys else "")
        ticker = link(r.ticker) + ("\\*" if r.price_mismatch else "")
        out.append(
            f"| {r.rank} | {delta} | {ticker} | {_md(r.company)[:32]} | {r.score:.1f} | {money(r.total_value)} | "
            f"{pct(r.pct_mcap, 2)} | {pct(r.delta_own, 0)} | {int(r.n_insiders)} | {_md(r.roles)} | "
            f"{int(r.buy_days)} | {r.last_buy} | {pct(r.price_chg, 0)} |"
        )
    out += [
        "",
        f"<sub>Δ = rank change since the previous filing day (🆕 newly ranked, ⬆️ entered the top {top_n} from #N, "
        "💵 new purchases filed). ΔOwn = average increase in each insider's holdings (capped at 100%). "
        "Chg = latest price vs. average buy price. \\* = filing prices aren't in the quote's currency/terms "
        "(ADR, foreign currency), so % MCap and Chg are hidden.</sub>",
        END_MARK,
    ]
    return "\n".join(out)


def update_readme(path: Path, section: str) -> bool:
    """Replace the generated block in the README. Returns True if the file changed."""
    text = path.read_text()
    pattern = re.compile(re.escape(START_MARK) + r".*?" + re.escape(END_MARK), re.S)
    if not pattern.search(text):
        raise SystemExit(f"{path}: markers {START_MARK} … {END_MARK} not found")
    new = pattern.sub(lambda _: section, text, count=1)
    if new != text:
        path.write_text(new)
    return new != text


# ---------------------------------------------------------------- static site

RANK_FIELDS = [
    "rank", "prev_rank", "ticker", "company", "issuer_cik", "score", "s_cost", "s_recurrence", "s_insiders",
    "total_value", "pct_mcap", "market_cap", "delta_own", "n_insiders", "weighted_insiders", "roles",
    "n_tx", "buy_days", "repeat_buyers", "first_buy", "last_buy", "avg_buy_price", "price", "price_asof",
    "price_chg", "price_mismatch",
]
PURCHASE_FIELDS = [
    "trans_date", "filing_date", "owner_name", "owner_role", "owner_title", "shares", "price", "value",
    "shares_after", "direct_indirect", "form_type", "accession",
]


def _clean(v):
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    if isinstance(v, (int, np.integer)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        f = float(v)
        return round(f, 6) if math.isfinite(f) else None
    if v is None or v is pd.NA or v is pd.NaT:
        return None
    return v


def site_payload(
    conn: sqlite3.Connection, as_of: date, ranked: pd.DataFrame, purchases: pd.DataFrame,
    repo: str, top_n: int = TOP_N,
) -> dict:
    p = RankParams()
    rows = [[_clean(v) for v in rec] for rec in ranked[RANK_FIELDS].itertuples(index=False)]
    detail = {}
    for cik, g in purchases.sort_values(["trans_date", "accession", "seq"], ascending=[False, True, True]).groupby("issuer_cik"):
        detail[str(int(cik))] = [[_clean(v) for v in rec] for rec in g[PURCHASE_FIELDS].itertuples(index=False)]
    ev = load_events(conn, as_of - timedelta(days=HISTORY_DAYS))
    ev_fields = ["as_of", "cik", "event", "ticker", "company", "rank", "prev_rank", "value", "detail"]
    events = [[_clean(v) for v in rec] for rec in ev[ev_fields].itertuples(index=False)]
    price_asof = conn.execute("SELECT MAX(price_asof) FROM market_data WHERE price_source = 'yahoo'").fetchone()[0]
    return {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of": as_of.isoformat(),
        "price_asof": price_asof,
        "window_days": p.window_days,
        "weights": {"cost": p.w_cost, "recurrence": p.w_recurrence, "insiders": p.w_insiders},
        "top_n": top_n,
        "big_move": BIG_MOVE,
        "repo": repo,
        "ranking": {"fields": RANK_FIELDS, "rows": rows},
        "purchases": {"fields": PURCHASE_FIELDS, "by_cik": detail},
        "events": {"fields": ev_fields, "rows": events},
    }


def write_site(out_dir: Path, payload: dict, ranked: pd.DataFrame) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    src = resources.files("insider_trade") / "site"
    for f in src.iterdir():
        if f.is_file():
            with resources.as_file(f) as path:
                shutil.copy(path, out_dir / f.name)
    (out_dir / "data.json").write_text(json.dumps(payload, separators=(",", ":"), allow_nan=False))
    csv = ranked[RANK_FIELDS].copy()
    csv.to_csv(out_dir / "ranking.csv", index=False, float_format="%.6g")
