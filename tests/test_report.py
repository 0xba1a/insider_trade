import json
from datetime import date

import pytest

from insider_trade import db
from insider_trade.rank import RankParams, rank
from insider_trade.report import (
    END_MARK, START_MARK, render_readme, site_payload, take_snapshot, update_readme, write_site,
)
from test_rank_and_data import add, ceo, director

D1, D2, D3 = date(2026, 11, 4), date(2026, 11, 5), date(2026, 11, 6)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


def events(conn, as_of):
    rows = conn.execute("SELECT ticker, event, rank, prev_rank FROM rank_events WHERE as_of = ?", (as_of.isoformat(),))
    return {(r[0], r[1]) for r in rows}


def test_filing_date_bounds_historical_ranking(conn):
    add(conn, 1, "OLD", ceo(11), [("2026-10-01", 100, 10, 200)], filing_date="2026-10-02")
    add(conn, 2, "LATE", ceo(21), [("2026-10-01", 100, 10, 200)], filing_date="2026-11-06")
    assert set(rank(conn, RankParams(as_of=D2)).ticker) == {"OLD"}
    assert set(rank(conn, RankParams(as_of=D3)).ticker) == {"OLD", "LATE"}


def test_decimal_slip_rows_dropped(conn):
    trades = [("2026-10-01", 100, 6.1, 1100), ("2026-10-02", 100, 6.2, 1200), ("2026-10-03", 100, 614.0, 1300)]
    add(conn, 1, "TYP", ceo(11), trades)
    r = rank(conn, RankParams(as_of=D3)).iloc[0]
    assert r.total_value == pytest.approx(1230)


def test_snapshot_events(conn):
    add(conn, 1, "AAA", ceo(11), [("2026-10-01", 1000, 10, 2000)], filing_date="2026-10-02")
    add(conn, 2, "BBB", director(21), [("2026-10-01", 100, 10, 1100)], filing_date="2026-10-02")
    ranked, _ = take_snapshot(conn, D1, top_n=1)
    assert ranked.ticker.tolist() == ["AAA", "BBB"]
    assert events(conn, D1) == set()  # no previous snapshot to compare with

    # BBB overtakes AAA with fresh buys from two more insiders; CCC appears for the first time.
    add(conn, 2, "BBB", ceo(22), [("2026-11-04", 5000, 10, 6000)], filing_date="2026-11-05")
    add(conn, 2, "BBB", director(23), [("2026-11-04", 5000, 10, 5000)], filing_date="2026-11-05")
    add(conn, 3, "CCC", director(31), [("2026-11-04", 10, 10, 1000)], filing_date="2026-11-05")
    ranked, _ = take_snapshot(conn, D2, top_n=1)
    assert ranked.ticker.tolist()[0] == "BBB"
    assert ranked.set_index("ticker").prev_rank.to_dict()["BBB"] == 2
    assert events(conn, D2) == {("BBB", "new_entry"), ("BBB", "new_buys"), ("CCC", "first_time")}

    # AAA climbs back the next day: it was in the top 1 recently, so it's not a "new" entry.
    add(conn, 1, "AAA", ceo(12), [("2026-11-05", 90000, 10, 90000)], filing_date="2026-11-06")
    add(conn, 1, "AAA", director(13), [("2026-11-05", 90000, 10, 90000)], filing_date="2026-11-06")
    add(conn, 1, "AAA", director(14), [("2026-11-05", 90000, 10, 90000)], filing_date="2026-11-06")
    ranked, _ = take_snapshot(conn, D3, top_n=1)
    assert ranked.ticker.tolist()[0] == "AAA"
    assert events(conn, D3) == {("AAA", "new_buys")}

    # Re-running a snapshot replaces it instead of duplicating rows.
    take_snapshot(conn, D3, top_n=1)
    assert conn.execute("SELECT COUNT(*) FROM rank_snapshots WHERE as_of = ?", (D3.isoformat(),)).fetchone()[0] == 3


def test_big_move(conn):
    for i in range(1, 9):
        add(conn, i, f"T{i}", director(i * 10), [("2026-10-01", 100 * (9 - i), 10, 10_000)], filing_date="2026-10-02")
    take_snapshot(conn, D1, top_n=10, big_move=5)
    add(conn, 8, "T8", ceo(81), [("2026-11-04", 50_000, 10, 50_000)], filing_date="2026-11-05")
    add(conn, 8, "T8", ceo(82), [("2026-11-04", 50_000, 10, 50_000)], filing_date="2026-11-05")
    ranked, _ = take_snapshot(conn, D2, top_n=10, big_move=5)
    assert ranked.ticker.iloc[0] == "T8"
    assert ("T8", "big_move") in events(conn, D2)


def test_readme_and_site(conn, tmp_path):
    add(conn, 1, "AAA", ceo(11), [("2026-10-01", 1000, 10, 2000)], filing_date="2026-10-02")
    take_snapshot(conn, D1)
    add(conn, 2, "B|B", director(21), [("2026-11-04", 100, 10, 1100)], filing_date="2026-11-05")
    ranked, purchases = take_snapshot(conn, D2)

    section = render_readme(conn, D2, ranked, "https://x.github.io/r/")
    assert section.startswith(START_MARK) and section.endswith(END_MARK)
    assert "[AAA](https://x.github.io/r/#AAA)" in section
    assert "B\\|B" in section and "🆕" in section

    readme = tmp_path / "README.md"
    readme.write_text(f"intro\n{START_MARK}\nSTALE-BLOCK\n{END_MARK}\nrest\n")
    assert update_readme(readme, section)
    assert not update_readme(readme, section)
    text = readme.read_text()
    assert text.startswith("intro\n") and text.endswith("\nrest\n") and "STALE-BLOCK" not in text

    site = tmp_path / "site"
    write_site(site, site_payload(conn, D2, ranked, purchases, "o/r"), ranked)
    data = json.loads((site / "data.json").read_text())
    assert data["as_of"] == "2026-11-05" and len(data["ranking"]["rows"]) == 2
    assert {e[2] for e in data["events"]["rows"]} == {"first_time", "new_entry", "new_buys"}
    assert (site / "index.html").exists() and (site / "ranking.csv").exists()
