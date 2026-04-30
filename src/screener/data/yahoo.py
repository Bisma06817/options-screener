"""Yahoo Finance fallback for earnings dates and company names.

Used only when tastytrade's `get_market_metrics` does not provide a value
(no `expected_report_date`). Per the client brief, Yahoo Finance is an
acceptable fallback as long as transient failures are retried with
exponential backoff and surfaced in the logs.

yfinance is unofficial — we treat every call as best-effort and never let
a Yahoo failure abort the whole scan.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from functools import lru_cache

import yfinance as yf
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)


class YahooClient:
    """Best-effort Yahoo Finance lookups with exponential backoff.

    Both methods retry up to 3 times on transient errors; if Yahoo is still
    unreachable, the caller gets None / the symbol back rather than an
    exception, so a single bad lookup never breaks the scan.
    """

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=8),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )
    def _fetch_calendar(self, symbol: str):
        return yf.Ticker(symbol).calendar

    def next_earnings_date(self, symbol: str) -> date | None:
        try:
            cal = self._fetch_calendar(symbol)
        except Exception as e:
            log.warning("Yahoo earnings lookup failed for %s after retries: %s", symbol, e)
            return None
        log.debug("Yahoo calendar response for %s: %s", symbol, cal)
        if not cal:
            return None
        raw = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if not raw:
            return None
        first = raw[0] if isinstance(raw, (list, tuple)) else raw
        if isinstance(first, datetime):
            return first.date()
        if isinstance(first, date):
            return first
        try:
            return date.fromisoformat(str(first)[:10])
        except (TypeError, ValueError):
            return None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=8),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )
    def _fetch_info(self, symbol: str) -> dict:
        return yf.Ticker(symbol).info or {}

    @lru_cache(maxsize=128)
    def company_name(self, symbol: str) -> str:
        try:
            info = self._fetch_info(symbol)
        except Exception as e:
            log.warning("Yahoo profile lookup failed for %s after retries: %s", symbol, e)
            return symbol
        name = info.get("longName") or info.get("shortName") or symbol
        log.debug("Yahoo company_name(%s) = %r", symbol, name)
        return name
