"""Pure-function screening pipeline.

Takes a list of contract dicts (already enriched with quotes/greeks) and
applies the filter chain. Lives behind no IO — easy to unit test.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class FilterParams:
    ivr_min: float
    dte_min: int
    dte_max: int
    delta_min: float
    delta_max: float


def passes_ivr(contract: dict, p: FilterParams) -> bool:
    ivr = contract.get("ivr")
    return ivr is not None and ivr >= p.ivr_min


def passes_dte(contract: dict, p: FilterParams) -> bool:
    dte = contract.get("dte")
    return dte is not None and p.dte_min <= dte <= p.dte_max


def passes_delta(contract: dict, p: FilterParams) -> bool:
    """Short-put delta is reported as a negative number by the streamer.
    The brief uses absolute magnitude (0.15..0.25)."""
    delta = contract.get("delta")
    if delta is None:
        return False
    return p.delta_min <= abs(delta) <= p.delta_max


def passes_earnings(contract: dict) -> bool:
    """Exclude if a known earnings date falls within the DTE window
    (today..expiry, inclusive)."""
    earnings: date | None = contract.get("earnings_date")
    if earnings is None:
        return True
    expiry: date = contract["expiry"]
    today: date = contract.get("scan_date") or date.today()
    return not (today <= earnings <= expiry)


def screen(contracts: list[dict], p: FilterParams) -> list[dict]:
    out = [
        c for c in contracts
        if passes_ivr(c, p)
        and passes_dte(c, p)
        and passes_delta(c, p)
        and passes_earnings(c)
    ]
    out.sort(key=lambda c: (c.get("ivr") or 0.0), reverse=True)
    return out


def expected_move(underlying_price: float, ivx: float | None, dte: int) -> float | None:
    """Expected Move = S * IVx * sqrt(DTE / 365). IVx as decimal (0.45 = 45%)."""
    if ivx is None or underlying_price <= 0 or dte <= 0:
        return None
    return underlying_price * ivx * math.sqrt(dte / 365.0)


def pop_from_delta(delta: float | None) -> float | None:
    """Probability-of-profit proxy for a short put = 1 - |delta|, in percent."""
    if delta is None:
        return None
    return (1.0 - abs(delta)) * 100.0
