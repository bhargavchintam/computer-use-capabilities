"""Synthetic members and shares. Every value here is fictional.

SSNs use the 900-range (never issued), phones use the 555-01xx range.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class Share:
    share_id: str
    description: str
    balance: Decimal
    available: Decimal
    status: str = "Active"
    rate: str = "0.05%"
    nickname: str = ""


@dataclass
class Member:
    number: str
    name: str
    ssn: str
    dob: str
    address: str
    phone: str
    restricted: bool = False
    shares: list[Share] = field(default_factory=list)

    def share(self, share_id: str) -> Share | None:
        return next((s for s in self.shares if s.share_id == share_id), None)


def _d(v: str) -> Decimal:
    return Decimal(v)


_PINECREST = [
    Member(
        "10042",
        "HARTWELL, JUNE M",
        "900-12-4417",
        "03/09/1984",
        "118 Alder Way, Pinecrest WA",
        "(206) 555-0142",
        shares=[
            Share("S01", "PRIMARY SAVINGS", _d("12450.31"), _d("12425.31"), rate="0.10%"),
            Share("S10", "SHARE DRAFT CHECKING", _d("2310.07"), _d("2310.07"), rate="0.00%"),
            Share("S50", "HOLIDAY CLUB", _d("600.00"), _d("600.00"), rate="0.25%"),
        ],
    ),
    Member(
        "10077",
        "OKAFOR, DANIEL T",
        "900-55-0183",
        "11/21/1990",
        "4 Cedar Ct, Pinecrest WA",
        "(206) 555-0177",
        shares=[
            Share("S01", "PRIMARY SAVINGS", _d("8093.44"), _d("8068.44"), rate="0.10%"),
            Share("S10", "SHARE DRAFT CHECKING", _d("512.90"), _d("512.90"), rate="0.00%"),
        ],
    ),
    Member(
        "10058",
        "LINDQVIST, ASTRID",
        "900-31-7720",
        "07/02/1976",
        "77 Spruce Rd, Pinecrest WA",
        "(206) 555-0158",
        shares=[Share("S01", "PRIMARY SAVINGS", _d("31207.00"), _d("31182.00"), rate="0.10%")],
    ),
    Member(
        "10013",
        "PRATT, MORGAN E",
        "900-44-2291",
        "01/15/1988",
        "9 Birch Ln, Pinecrest WA",
        "(206) 555-0113",
        restricted=True,
        shares=[Share("S01", "PRIMARY SAVINGS", _d("4400.00"), _d("4375.00"), rate="0.10%")],
    ),
    Member(  # checking only: "what is the savings balance?" has a legitimate answer of "none"
        "10091",
        "VOSS, ADRIAN K",
        "900-67-3348",
        "05/27/1995",
        "212 Hemlock St, Pinecrest WA",
        "(206) 555-0191",
        shares=[Share("S10", "SHARE DRAFT CHECKING", _d("1250.00"), _d("1250.00"), rate="0.00%")],
    ),
]

_LAKESIDE = [
    Member(
        "20031",
        "MORALES, INES R",
        "900-67-1045",
        "05/30/1982",
        "310 Shore Dr, Lakeside MN",
        "(612) 555-0131",
        shares=[
            Share("S01", "PRIMARY SAVINGS", _d("5620.18"), _d("5595.18"), rate="0.15%"),
            Share("S10", "SHARE DRAFT CHECKING", _d("940.55"), _d("940.55"), rate="0.00%"),
        ],
    ),
    Member(
        "20064",
        "BAKER, THOMAS W",
        "900-23-9981",
        "09/12/1969",
        "12 Harbor St, Lakeside MN",
        "(612) 555-0164",
        shares=[Share("S01", "PRIMARY SAVINGS", _d("18300.75"), _d("18275.75"), rate="0.15%")],
    ),
    Member(
        "20013",
        "CHEN, LILY A",
        "900-81-3307",
        "12/03/1993",
        "5 Pier Ave, Lakeside MN",
        "(612) 555-0113",
        restricted=True,
        shares=[Share("S01", "PRIMARY SAVINGS", _d("2100.00"), _d("2075.00"), rate="0.15%")],
    ),
]

_SEED: dict[str, list[Member]] = {"pinecrest": _PINECREST, "lakeside": _LAKESIDE}


def fresh_members(tenant_id: str) -> dict[str, Member]:
    return {m.number: m for m in copy.deepcopy(_SEED[tenant_id])}


def money(v: Decimal) -> str:
    return f"${v:,.2f}"
