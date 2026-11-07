from pathlib import Path

from insider_trade.ingest import parse_form_index
from insider_trade.models import Owner
from insider_trade.parser import parse_submission
from insider_trade.roles import representative, role_label, role_weight

FIXTURE = Path(__file__).parent / "fixtures" / "form4.xml"


def _submission() -> str:
    return "<SEC-DOCUMENT>header\n<XML>\n" + FIXTURE.read_text() + "\n</XML>\n</SEC-DOCUMENT>"


def test_parse_submission_keeps_only_open_market_purchases():
    f = parse_submission(_submission(), "0000000000-26-000001", "2026-11-05", "4")
    assert f.issuer_cik == 123456
    assert f.issuer_symbol == "EXMP"
    assert f.aff10b5one is False
    assert [p.seq for p in f.purchases] == [1, 2]
    p1, p2 = f.purchases
    assert (p1.trans_date, p1.shares, p1.price, p1.shares_after, p1.direct_indirect) == (
        "2026-11-03", 1000.0, 12.5, 11500.0, "D")
    assert p2.price is None  # footnote-only price
    assert p2.nature == "By Trust"


def test_parse_owners_and_representative():
    f = parse_submission(_submission(), "a", "2026-11-05")
    assert len(f.owners) == 2
    ceo, trust = f.owners
    assert ceo.cik == 999001 and ceo.is_officer and ceo.is_director and ceo.title == "Chief Executive Officer"
    assert trust.is_ten_pct and not trust.is_officer
    assert representative(f.owners) is ceo


def test_role_weights_and_labels():
    assert role_label(Owner(1, "x", is_officer=True, title="President & CEO")) == "CEO"
    assert role_label(Owner(1, "x", is_officer=True, title="EVP, Chief Financial Officer")) == "CFO"
    assert role_label(Owner(1, "x", is_officer=True, title="Senior Vice President")) == "Officer"
    assert role_label(Owner(1, "x", is_director=True)) == "Dir"
    assert role_label(Owner(1, "x", is_ten_pct=True)) == "10%"
    assert role_weight(Owner(1, "x", is_officer=True, title="President")) == 3.0
    assert role_weight(Owner(1, "x", is_officer=True, title="Vice President")) == 2.0
    assert role_weight(Owner(1, "x", is_director=True, is_ten_pct=True)) == 1.5
    assert role_weight(Owner(1, "x")) == 0.5


def test_parse_form_index_dedupes_by_accession():
    idx = """Form Type   Company Name   CIK   Date Filed  File Name
---------------------------------------------------------------
4                ACME CORP                                        111     20261105    edgar/data/111/0000000111-26-000001.txt
4                SMITH JOHN                                       222     20261105    edgar/data/222/0000000111-26-000001.txt
4/A              BETA INC                                         333     20261105    edgar/data/333/0000000333-26-000009.txt
424B2            GAMMA BANK                                       444     20261105    edgar/data/444/0000000444-26-000002.txt
40-F             DELTA LTD                                        555     20261105    edgar/data/555/0000000555-26-000003.txt
"""
    got = parse_form_index(idx)
    assert got == [
        ("0000000111-26-000001", "4", [
            "edgar/data/111/0000000111-26-000001.txt", "edgar/data/222/0000000111-26-000001.txt"]),
        ("0000000333-26-000009", "4/A", ["edgar/data/333/0000000333-26-000009.txt"]),
    ]
