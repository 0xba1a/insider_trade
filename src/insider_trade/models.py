from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Owner:
    cik: int
    name: str
    is_director: bool = False
    is_officer: bool = False
    is_ten_pct: bool = False
    is_other: bool = False
    title: str = ""


@dataclass
class Purchase:
    seq: int
    security_title: str
    trans_date: str  # ISO yyyy-mm-dd
    shares: float | None
    price: float | None
    shares_after: float | None
    direct_indirect: str = ""
    nature: str = ""


@dataclass
class Filing:
    accession: str
    form_type: str
    filing_date: str  # ISO yyyy-mm-dd
    period: str
    issuer_cik: int
    issuer_name: str
    issuer_symbol: str
    aff10b5one: bool | None
    owners: list[Owner] = field(default_factory=list)
    purchases: list[Purchase] = field(default_factory=list)
