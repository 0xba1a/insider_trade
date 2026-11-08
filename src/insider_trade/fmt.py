"""Number formatting shared by the CLI and the published report."""
from __future__ import annotations

import math


def _missing(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v))


def money(v: float | None) -> str:
    if _missing(v):
        return "-"
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= div:
            return f"${v / div:.1f}{unit}"
    return f"${v:,.0f}"


def pct(v: float | None, digits: int = 1) -> str:
    if _missing(v):
        return "-"
    if math.isinf(v):
        return "new"
    return f"{v * 100:.{digits}f}%"


def num(v: float | None, fmt: str = "{:,.2f}") -> str:
    if _missing(v):
        return "-"
    return fmt.format(v)
