"""Parse Form 4 ownershipDocument XML into Filing objects (open-market purchases only)."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from .models import Filing, Owner, Purchase

_DOC_RE = re.compile(r"<ownershipDocument>.*?</ownershipDocument>", re.S)
_BAD_SYMBOLS = {"", "N/A", "NA", "NONE", "NULL", "-", "--"}


class ParseError(Exception):
    pass


def _text(el: ET.Element | None, path: str) -> str:
    if el is None:
        return ""
    node = el.find(path)
    if node is None or node.text is None:
        return ""
    return node.text.strip()


def _value(el: ET.Element | None, path: str) -> str:
    """Form 4 wraps most fields as <field><value>..</value><footnoteId/></field>."""
    return _text(el, f"{path}/value")


def _bool(s: str) -> bool:
    return s.strip().lower() in ("1", "true", "y", "yes")


def _float(s: str) -> float | None:
    s = (s or "").replace(",", "").replace("$", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def clean_symbol(sym: str) -> str:
    s = (sym or "").strip().upper()
    return "" if s in _BAD_SYMBOLS else s


def parse_owner(el: ET.Element) -> Owner:
    rel = el.find("reportingOwnerRelationship")
    title = _text(rel, "officerTitle") or _text(rel, "otherText")
    return Owner(
        cik=int(_text(el, "reportingOwnerId/rptOwnerCik") or 0),
        name=_text(el, "reportingOwnerId/rptOwnerName"),
        is_director=_bool(_text(rel, "isDirector")),
        is_officer=_bool(_text(rel, "isOfficer")),
        is_ten_pct=_bool(_text(rel, "isTenPercentOwner")),
        is_other=_bool(_text(rel, "isOther")),
        title=title,
    )


def parse_ownership_document(xml: str, accession: str, filing_date: str, form_type: str | None = None) -> Filing:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise ParseError(f"{accession}: {e}") from e

    issuer = root.find("issuer")
    aff = _text(root, "aff10b5One")
    filing = Filing(
        accession=accession,
        form_type=form_type or _text(root, "documentType"),
        filing_date=filing_date,
        period=_text(root, "periodOfReport"),
        issuer_cik=int(_text(issuer, "issuerCik") or 0),
        issuer_name=_text(issuer, "issuerName"),
        issuer_symbol=clean_symbol(_text(issuer, "issuerTradingSymbol")),
        aff10b5one=_bool(aff) if aff else None,
        owners=[parse_owner(o) for o in root.findall("reportingOwner")],
    )

    table = root.find("nonDerivativeTable")
    if table is not None:
        for seq, tx in enumerate(table.findall("nonDerivativeTransaction")):
            if _text(tx, "transactionCoding/transactionCode").upper() != "P":
                continue
            if _value(tx, "transactionAmounts/transactionAcquiredDisposedCode").upper() == "D":
                continue
            filing.purchases.append(
                Purchase(
                    seq=seq,
                    security_title=_value(tx, "securityTitle"),
                    trans_date=_value(tx, "transactionDate")[:10],
                    shares=_float(_value(tx, "transactionAmounts/transactionShares")),
                    price=_float(_value(tx, "transactionAmounts/transactionPricePerShare")),
                    shares_after=_float(_value(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction")),
                    direct_indirect=_value(tx, "ownershipNature/directOrIndirectOwnership"),
                    nature=_value(tx, "ownershipNature/natureOfOwnership"),
                )
            )
    return filing


def parse_submission(text: str, accession: str, filing_date: str, form_type: str | None = None) -> Filing:
    """Parse a full EDGAR submission (.txt) containing a Form 4 XML document."""
    m = _DOC_RE.search(text)
    if not m:
        raise ParseError(f"{accession}: no ownershipDocument found")
    return parse_ownership_document(m.group(0), accession, filing_date, form_type)
