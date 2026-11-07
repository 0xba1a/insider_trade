"""Insider role classification and weighting."""
from __future__ import annotations

import re

from .models import Owner

WEIGHT_TOP_EXEC = 3.0  # CEO / CFO / President
WEIGHT_OFFICER = 2.0
WEIGHT_DIRECTOR = 1.5
WEIGHT_TEN_PCT = 0.5
WEIGHT_OTHER = 0.5

_CEO = re.compile(r"\bceo\b|chief executive|\bco-ceo\b")
_CFO = re.compile(r"\bcfo\b|chief financial")
_PRESIDENT = re.compile(r"\bpresident\b")
_VICE = re.compile(r"vice|\b[sea]?vp\b")


def _norm(title: str) -> str:
    return (title or "").lower()


def is_ceo(title: str) -> bool:
    t = _norm(title)
    return bool(_CEO.search(t)) or (bool(_PRESIDENT.search(t)) and not _VICE.search(t))


def is_cfo(title: str) -> bool:
    return bool(_CFO.search(_norm(title)))


def role_weight(o: Owner) -> float:
    w = 0.0
    if o.is_officer:
        w = WEIGHT_TOP_EXEC if (is_ceo(o.title) or is_cfo(o.title)) else WEIGHT_OFFICER
    if o.is_director:
        w = max(w, WEIGHT_DIRECTOR)
    if o.is_ten_pct:
        w = max(w, WEIGHT_TEN_PCT)
    if o.is_other or w == 0.0:
        w = max(w, WEIGHT_OTHER)
    return w


def role_label(o: Owner) -> str:
    """Short role tag used in reports: CEO, CFO, Officer, Dir, 10%, Other."""
    if o.is_officer:
        if is_ceo(o.title):
            return "CEO"
        if is_cfo(o.title):
            return "CFO"
        return "Officer"
    if o.is_director:
        return "Dir"
    if o.is_ten_pct:
        return "10%"
    return "Other"


def representative(owners: list[Owner]) -> Owner | None:
    """Joint filings (e.g. a fund and its GP) report the same trade once per owner;
    attribute the trade to the most senior owner so it isn't counted multiple times."""
    if not owners:
        return None
    return max(owners, key=role_weight)  # max() keeps the first owner on ties
