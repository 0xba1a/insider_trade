import io
import math
import zipfile
from datetime import date

import pytest

from insider_trade import db
from insider_trade.dataset import filings_from_zip, quarter_bounds
from insider_trade.market import latest_shares
from insider_trade.models import Filing, Owner, Purchase
from insider_trade.rank import RankParams, rank, ticker_detail

AS_OF = date(2026, 11, 6)
_acc = iter(range(1, 10_000))


def ceo(cik):
    return Owner(cik, f"CEO {cik}", is_officer=True, title="CEO")


def director(cik):
    return Owner(cik, f"Dir {cik}", is_director=True)


def fund(cik):
    return Owner(cik, f"Fund {cik}", is_ten_pct=True)


def add(conn, issuer, ticker, owner, trades, form_type="4", filing_date="2026-11-05", accession=None):
    """trades: list of (trans_date, shares, price, shares_after)."""
    accession = accession or f"0000000000-26-{next(_acc):06d}"
    f = Filing(
        accession=accession, form_type=form_type, filing_date=filing_date, period=trades[0][0],
        issuer_cik=issuer, issuer_name=f"Issuer {issuer}", issuer_symbol=ticker, aff10b5one=False,
        owners=[owner],
        purchases=[Purchase(i, "Common Stock", d, s, p, a, "D") for i, (d, s, p, a) in enumerate(trades)],
    )
    db.save_filing(conn, f, "test")
    return accession


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


def test_cluster_buying_outranks_single_buy(conn):
    # AAA: CEO + 2 directors, repeat buying on several days
    add(conn, 1, "AAA", ceo(11), [("2026-10-01", 1000, 10, 2000), ("2026-10-15", 1000, 10, 3000)])
    add(conn, 1, "AAA", director(12), [("2026-10-02", 500, 10, 1000)])
    add(conn, 1, "AAA", director(13), [("2026-10-20", 500, 10, 500)])
    # BBB: one director, one buy, small
    add(conn, 2, "BBB", director(21), [("2026-10-05", 100, 10, 10100)])
    # CCC: one officer, one larger buy
    add(conn, 3, "CCC", Owner(31, "VP", is_officer=True, title="VP Sales"), [("2026-10-05", 2000, 10, 50000)])

    df = rank(conn, RankParams(as_of=AS_OF))
    assert list(df.ticker) == ["AAA", "CCC", "BBB"]
    a = df.iloc[0]
    assert a.n_insiders == 3 and a.buy_days == 4 and a.repeat_buyers == 1
    assert a.total_value == pytest.approx(30_000)
    assert a.weighted_insiders == pytest.approx(3 + 1.5 + 1.5)
    assert a.roles == "CEO, 2 Dir"
    # CEO had 1000 before -> bought 2000 (capped 100%); dir12 had 500 -> 100%; dir13 new -> 100%
    assert a.delta_own == pytest.approx(1.0)


def test_funds_excluded_by_default_and_window_respected(conn):
    add(conn, 4, "FND", fund(41), [("2026-10-10", 1_000_000, 50, 9_000_000)])
    add(conn, 5, "OLD", ceo(51), [("2026-01-10", 1000, 10, 2000)])
    assert rank(conn, RankParams(as_of=AS_OF)).empty
    df = rank(conn, RankParams(as_of=AS_OF, include_funds=True))
    assert list(df.ticker) == ["FND"]
    assert list(rank(conn, RankParams(as_of=AS_OF, window_days=365)).ticker) == ["OLD"]


def test_amendment_supersedes_original(conn):
    add(conn, 6, "AMD", ceo(61), [("2026-10-10", 1000, 10, 2000)], filing_date="2026-10-12")
    add(conn, 6, "AMD", ceo(61), [("2026-10-10", 1500, 10, 2500)], form_type="4/A", filing_date="2026-10-20")
    df = rank(conn, RankParams(as_of=AS_OF))
    assert df.iloc[0].total_value == pytest.approx(15_000)
    assert len(ticker_detail(conn, "amd", RankParams(as_of=AS_OF))) == 1


def test_same_trade_reported_by_related_filers_counted_once(conn):
    add(conn, 10, "DUP", fund(101), [("2026-10-10", 1_000_000, 15, 1_348_825)])
    add(conn, 10, "DUP", director(102), [("2026-10-10", 1_000_000, 15, 1_348_825)])
    add(conn, 10, "DUP", ceo(103), [("2026-10-10", 1_000, 15, 5_000)])
    r = rank(conn, RankParams(as_of=AS_OF, include_funds=True)).iloc[0]
    assert r.total_value == pytest.approx(15_015_000)
    assert r.n_insiders == 2 and r.roles == "CEO, Dir"


def test_unlisted_and_zero_price_rows_ignored(conn):
    add(conn, 7, "", ceo(71), [("2026-10-10", 1000, 10, 2000)])
    add(conn, 8, "ZRO", ceo(81), [("2026-10-10", 1000, None, 2000), ("2026-10-11", 0, 5, 2000)])
    assert rank(conn, RankParams(as_of=AS_OF)).empty
    assert len(rank(conn, RankParams(as_of=AS_OF, include_unlisted=True))) == 1


def test_market_cap_normalization(conn):
    add(conn, 9, "MKT", ceo(91), [("2026-10-10", 1000, 10, 2000)])
    conn.execute(
        "INSERT INTO market_data VALUES (9, 'MKT', 1000000, '2026-09-30', 'x', 12.5, '2026-11-06', 'yahoo')"
    )
    r = rank(conn, RankParams(as_of=AS_OF)).iloc[0]
    assert r.market_cap == pytest.approx(12_500_000)
    assert r.pct_mcap == pytest.approx(10_000 / 12_500_000)
    assert r.price_chg == pytest.approx(0.25)


def test_latest_shares_sums_classes_from_latest_filing():
    facts = [
        {"end": "2026-06-30", "val": 100, "accn": "a1", "filed": "2026-08-01"},
        {"end": "2026-09-30", "val": 70, "accn": "a2", "filed": "2026-11-01"},
        {"end": "2026-09-30", "val": 30, "accn": "a2", "filed": "2026-11-01"},
    ]
    assert latest_shares(facts, sum_classes=True) == (100.0, "2026-09-30")
    assert latest_shares(facts, sum_classes=False) == (70.0, "2026-09-30")
    assert latest_shares([], True) is None


def test_quarter_bounds():
    assert quarter_bounds(2026, 1) == (date(2026, 1, 1), date(2026, 3, 31))
    assert quarter_bounds(2026, 4) == (date(2026, 10, 1), date(2026, 12, 31))


def test_filings_from_zip():
    def tsv(header, *rows):
        return "\t".join(header) + "\n" + "".join("\t".join(r) + "\n" for r in rows)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("SUBMISSION.tsv", tsv(
            ["ACCESSION_NUMBER", "FILING_DATE", "PERIOD_OF_REPORT", "DOCUMENT_TYPE", "ISSUERCIK",
             "ISSUERNAME", "ISSUERTRADINGSYMBOL", "AFF10B5ONE"],
            ["A1", "31-JUL-2026", "29-JUL-2026", "4", "0000000111", "Acme", "ACME", "0"],
            ["A2", "31-JUL-2026", "29-JUL-2026", "4", "0000000222", "Beta", "N/A", ""],
        ))
        z.writestr("REPORTINGOWNER.tsv", tsv(
            ["ACCESSION_NUMBER", "RPTOWNERCIK", "RPTOWNERNAME", "RPTOWNER_RELATIONSHIP", "RPTOWNER_TITLE", "RPTOWNER_TXT"],
            ["A1", "0000000901", "Smith", "Director,Officer", "CFO", ""],
        ))
        z.writestr("NONDERIV_TRANS.tsv", tsv(
            ["ACCESSION_NUMBER", "NONDERIV_TRANS_SK", "SECURITY_TITLE", "TRANS_DATE", "TRANS_CODE", "TRANS_SHARES",
             "TRANS_PRICEPERSHARE", "TRANS_ACQUIRED_DISP_CD", "SHRS_OWND_FOLWNG_TRANS",
             "DIRECT_INDIRECT_OWNERSHIP", "NATURE_OF_OWNERSHIP"],
            ["A1", "5", "Common", "29-JUL-2026", "P", "100.0", "9.5", "A", "1100.0", "D", ""],
            ["A1", "6", "Common", "29-JUL-2026", "S", "50.0", "9.5", "D", "1050.0", "D", ""],
            ["A2", "7", "Common", "28-JUL-2026", "A", "10.0", "0", "A", "10.0", "D", ""],
        ))
    filings = filings_from_zip(buf.getvalue())
    assert len(filings) == 1
    f = filings[0]
    assert (f.accession, f.filing_date, f.issuer_cik, f.issuer_symbol, f.aff10b5one) == (
        "A1", "2026-07-31", 111, "ACME", False)
    assert f.owners[0].is_officer and f.owners[0].is_director and f.owners[0].title == "CFO"
    (p,) = f.purchases
    assert (p.trans_date, p.shares, p.price, p.shares_after) == ("2026-07-29", 100.0, 9.5, 1100.0)
    assert not math.isnan(p.price)
