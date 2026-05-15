"""Pure-function screening pipeline.

Takes a list of contract dicts (already enriched with quotes/greeks) and
applies the filter chain. Lives behind no IO — easy to unit test.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date

log = logging.getLogger(__name__)


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
    Compared against absolute magnitude."""
    delta = contract.get("delta")
    if delta is None:
        return False
    return p.delta_min <= abs(delta) <= p.delta_max


def is_third_friday(d: date) -> bool:
    # Standard monthly equity options expire on the 3rd Friday of the month.
    # When the 3rd Friday is a US market holiday (e.g. Juneteenth Jun 19 2026,
    # Good Friday) the OCC shifts that month's monthly to the prior Thursday,
    # and weekly Thursday expiries do not exist on equities, so accepting
    # Thursday-in-day-15-to-21 is a safe heuristic for the holiday-shifted
    # monthly without admitting any weekly contracts.
    return d.weekday() in (3, 4) and 15 <= d.day <= 21


def passes_monthly(contract: dict) -> bool:
    expiry: date | None = contract.get("expiry")
    return expiry is not None and is_third_friday(expiry)


def _drop_reason(c: dict, p: FilterParams) -> str | None:
    """First filter the contract fails, or None if it passes all."""
    if not passes_ivr(c, p):
        return f"ivr={c.get('ivr')!r} (need >={p.ivr_min})"
    if not passes_dte(c, p):
        return f"dte={c.get('dte')!r} (need {p.dte_min}-{p.dte_max})"
    if not passes_delta(c, p):
        d = c.get("delta")
        return f"delta={d!r} |d|={abs(d) if d is not None else None} (need {p.delta_min}-{p.delta_max})"
    if not passes_monthly(c):
        return f"non-monthly expiry={c.get('expiry')!r}"
    return None


def screen(contracts: list[dict], p: FilterParams) -> list[dict]:
    out = []
    for c in contracts:
        reason = _drop_reason(c, p)
        if reason is None:
            out.append(c)
        else:
            log.info(
                "filter drop: %s exp=%s strike=%sP — %s",
                c.get("symbol"), c.get("expiry"), c.get("strike"), reason,
            )
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
