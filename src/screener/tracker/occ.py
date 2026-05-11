"""Parse OSI 21-char option symbols.

Standard format: ROOT YYMMDD[C|P]NNNNNNNN — single space between the
root and the date, strike encoded as int(strike * 1000) zero-padded to
8 digits.

  Example: 'AVGO 260515P00270000' -> AVGO, 2026-05-15, put, strike 270.0

Stan's legacy tracker uses a variant with double spaces and a trailing
(YYYYMMDD) purchase-date suffix — those are accepted too:

  Example: 'AVGO  260515P00270000 (20260317)' -> same as above.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

# Symbol + (one or more whitespace) + YYMMDD + C/P + 8-digit strike
# Optionally followed by whitespace and a "(YYYYMMDD)" purchase-date
# suffix (Stan's legacy format). The suffix is matched but discarded —
# Purchase Date lives in its own column on the tracker sheet.
_OCC_RE = re.compile(
    r"^([A-Z][A-Z0-9.]{0,5})\s+(\d{6})([CP])(\d{8})\s*(?:\(\d{8}\))?$"
)


@dataclass(frozen=True)
class ParsedOcc:
    symbol: str
    option_type: str  # "C" or "P"
    strike: float
    expiration: date

    def as_instrument_spec(self) -> dict:
        """Shape expected by tasty-agent get_quotes / get_greeks."""
        return {
            "symbol": self.symbol,
            "option_type": self.option_type,
            "strike_price": self.strike,
            "expiration_date": self.expiration.isoformat(),
        }


def parse(occ: str) -> ParsedOcc:
    m = _OCC_RE.match(occ.strip().upper())
    if not m:
        raise ValueError(f"Not a valid OSI option symbol: {occ!r}")
    symbol, yymmdd, cp, strike_str = m.groups()
    year = 2000 + int(yymmdd[0:2])
    month = int(yymmdd[2:4])
    day = int(yymmdd[4:6])
    return ParsedOcc(
        symbol=symbol,
        option_type=cp,
        strike=int(strike_str) / 1000.0,
        expiration=date(year, month, day),
    )
