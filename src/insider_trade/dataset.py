"""Historical backfill from SEC 'Insider Transactions Data Sets' (quarterly Form 3/4/5 extracts)."""
from __future__ import annotations

import csv
import io
import logging
import re
import sqlite3
import zipfile
from datetime import date, datetime, timedelta

import pandas as pd

from . import db
from .edgar import SecClient
from .models import Filing, Owner, Purchase
from .parser import clean_symbol

log = logging.getLogger(__name__)

DATASETS_PAGE = "https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets"
_ZIP_RE = re.compile(r'href="([^"]*?(\d{4})q([1-4])_form345\.zip)"')


def quarter_bounds(year: int, q: int) -> tuple[date, date]:
    start = date(year, 3 * (q - 1) + 1, 1)
    end = date(year + (q == 4), (3 * q) % 12 + 1, 1) - timedelta(days=1)
    return start, end


def list_quarters(client: SecClient) -> dict[str, str]:
    """Map 'YYYYqN' -> absolute zip URL for every published quarter."""
    html = client.get_text(DATASETS_PAGE)
    out = {}
    for href, y, q in _ZIP_RE.findall(html):
        url = href if href.startswith("http") else "https://www.sec.gov" + href
        out[f"{y}q{q}"] = url
    return out


def _read_tsv(zf: zipfile.ZipFile, name: str, usecols: list[str]) -> pd.DataFrame:
    with zf.open(name) as fh:
        return pd.read_csv(
            fh, sep="\t", dtype=str, usecols=usecols, keep_default_na=False,
            quoting=csv.QUOTE_NONE, encoding="utf-8", encoding_errors="replace",
        )


def _iso(d: str) -> str:
    d = (d or "").strip()
    if not d:
        return ""
    for fmt in ("%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(d, fmt).date().isoformat()
        except ValueError:
            pass
    return d


def _num(s: str) -> float | None:
    try:
        return float(s) if s not in ("", None) else None
    except ValueError:
        return None


def _bool(s: str) -> bool | None:
    s = (s or "").strip().lower()
    if not s:
        return None
    return s in ("1", "true", "y", "yes")


def filings_from_zip(data: bytes) -> list[Filing]:
    zf = zipfile.ZipFile(io.BytesIO(data))
    tx = _read_tsv(zf, "NONDERIV_TRANS.tsv", [
        "ACCESSION_NUMBER", "NONDERIV_TRANS_SK", "SECURITY_TITLE", "TRANS_DATE", "TRANS_CODE",
        "TRANS_SHARES", "TRANS_PRICEPERSHARE", "TRANS_ACQUIRED_DISP_CD", "SHRS_OWND_FOLWNG_TRANS",
        "DIRECT_INDIRECT_OWNERSHIP", "NATURE_OF_OWNERSHIP",
    ])
    tx = tx[(tx.TRANS_CODE.str.upper() == "P") & (tx.TRANS_ACQUIRED_DISP_CD.str.upper() != "D")]
    accs = set(tx.ACCESSION_NUMBER)

    sub = _read_tsv(zf, "SUBMISSION.tsv", [
        "ACCESSION_NUMBER", "FILING_DATE", "PERIOD_OF_REPORT", "DOCUMENT_TYPE", "ISSUERCIK",
        "ISSUERNAME", "ISSUERTRADINGSYMBOL", "AFF10B5ONE",
    ])
    sub = sub[sub.ACCESSION_NUMBER.isin(accs) & sub.DOCUMENT_TYPE.isin(["4", "4/A"])]

    own = _read_tsv(zf, "REPORTINGOWNER.tsv", [
        "ACCESSION_NUMBER", "RPTOWNERCIK", "RPTOWNERNAME", "RPTOWNER_RELATIONSHIP",
        "RPTOWNER_TITLE", "RPTOWNER_TXT",
    ])
    own = own[own.ACCESSION_NUMBER.isin(set(sub.ACCESSION_NUMBER))]

    owners_by_acc: dict[str, list[Owner]] = {}
    for r in own.itertuples(index=False):
        rel = {x.strip().lower() for x in r.RPTOWNER_RELATIONSHIP.split(",")}
        owners_by_acc.setdefault(r.ACCESSION_NUMBER, []).append(Owner(
            cik=int(r.RPTOWNERCIK or 0), name=r.RPTOWNERNAME,
            is_director="director" in rel, is_officer="officer" in rel,
            is_ten_pct="tenpercentowner" in rel, is_other="other" in rel,
            title=r.RPTOWNER_TITLE or r.RPTOWNER_TXT,
        ))

    tx = tx.assign(_sk=pd.to_numeric(tx.NONDERIV_TRANS_SK, errors="coerce")).sort_values("_sk")
    tx_by_acc: dict[str, list[Purchase]] = {}
    for r in tx.itertuples(index=False):
        lst = tx_by_acc.setdefault(r.ACCESSION_NUMBER, [])
        lst.append(Purchase(
            seq=len(lst), security_title=r.SECURITY_TITLE, trans_date=_iso(r.TRANS_DATE),
            shares=_num(r.TRANS_SHARES), price=_num(r.TRANS_PRICEPERSHARE),
            shares_after=_num(r.SHRS_OWND_FOLWNG_TRANS),
            direct_indirect=r.DIRECT_INDIRECT_OWNERSHIP, nature=r.NATURE_OF_OWNERSHIP,
        ))

    filings = []
    for r in sub.itertuples(index=False):
        filings.append(Filing(
            accession=r.ACCESSION_NUMBER, form_type=r.DOCUMENT_TYPE, filing_date=_iso(r.FILING_DATE),
            period=_iso(r.PERIOD_OF_REPORT), issuer_cik=int(r.ISSUERCIK or 0), issuer_name=r.ISSUERNAME,
            issuer_symbol=clean_symbol(r.ISSUERTRADINGSYMBOL), aff10b5one=_bool(r.AFF10B5ONE),
            owners=owners_by_acc.get(r.ACCESSION_NUMBER, []),
            purchases=tx_by_acc.get(r.ACCESSION_NUMBER, []),
        ))
    return filings


def ingest_quarter(conn: sqlite3.Connection, client: SecClient, quarter: str, url: str) -> int:
    log.info("downloading %s", url)
    data = client.get(url, timeout=300).content
    filings = filings_from_zip(data)
    n = sum(db.save_filing(conn, f, source="dataset") for f in filings)

    # Mark the quarter's filing dates as covered so daily ingestion won't refetch them.
    y, q = int(quarter[:4]), int(quarter[-1])
    start, end = quarter_bounds(y, q)
    d = start
    while d <= end:
        if d.weekday() < 5:
            conn.execute(
                "INSERT OR IGNORE INTO ingest_log VALUES (?,?,?,?,?)",
                (d.isoformat(), "dataset", None, None, db.now_iso()),
            )
        d += timedelta(days=1)
    conn.execute("INSERT OR REPLACE INTO dataset_log VALUES (?,?,?)", (quarter, len(filings), db.now_iso()))
    conn.commit()
    log.info("%s: %d filings with purchases, %d purchase rows", quarter, len(filings), n)
    return n


def backfill(conn: sqlite3.Connection, start: date, end: date, client: SecClient | None = None, force: bool = False) -> date:
    """Load all published quarters overlapping [start, end]. Returns the first date
    not covered by a quarterly data set (to be filled from the daily index)."""
    client = client or SecClient()
    quarters = list_quarters(client)
    done = {r[0] for r in conn.execute("SELECT quarter FROM dataset_log")}
    covered_until = start
    for quarter in sorted(quarters):
        qs, qe = quarter_bounds(int(quarter[:4]), int(quarter[-1]))
        if qe < start or qs > end:
            continue
        if force or quarter not in done:
            ingest_quarter(conn, client, quarter, quarters[quarter])
        covered_until = max(covered_until, qe + timedelta(days=1))
    return covered_until
