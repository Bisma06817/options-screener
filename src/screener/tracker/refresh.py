"""Daily refresh of every OPEN position in the tracker sheet.

For each row on the main tracker tab with Status=OPEN, this:
  1. Parses the OCC option name.
  2. Streams a live quote for the option (bid/ask) and the underlying.
  3. Pulls IVR / IVx from market metrics; VIX from the live index quote.
  4. Computes P&L (= price_paid - current_price for a short put), Range
     (Expected Move using IVx and remaining DTE), and Limit
     (= price_paid * 0.5; the 50%-of-premium buy-back target Stan
     described in his walkthrough).
  5. Updates the main tracker row with the latest dynamic values.
  6. Appends a new row to that option's dedicated per-option tab.

Uses the tastytrade Python SDK + tasty-agent's DXLink streaming helper
directly — no MCP / LLM round-trip. Deterministic, cheap, fast.
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import date
from typing import Any

from tastytrade import Session
from tastytrade.dxfeed import Quote
from tastytrade.metrics import get_market_metrics

from tasty_agent.market_data import stream_events
from tasty_agent.orders import InstrumentSpec, get_instrument_details

from ..config import Env
from .occ import parse as parse_occ
from .sheets_tracker import TrackerClient

log = logging.getLogger(__name__)

_QUOTE_TIMEOUT_SEC = 15.0
_CHUNK_SIZE = 10


async def _gather_market_data(
    env: Env,
    parsed_positions: list[tuple[dict, Any]],
) -> dict[str, Any]:
    """Open one tastytrade session, fetch quotes for all positions +
    underlyings + VIX, and market metrics for all unique symbols.

    Returns a dict with:
      - quote_by_label: { "opt:<OCC>" | "stock:<SYM>" | "index:VIX" -> Quote }
      - metrics_by_sym: { symbol -> MarketMetricInfo }
    """
    # Build a labelled instrument list so we can map quotes back by purpose.
    labelled: list[tuple[str, InstrumentSpec]] = []
    symbols: set[str] = set()
    for position, occ in parsed_positions:
        symbols.add(occ.symbol)
        labelled.append((
            f"opt:{position['OCC']}",
            InstrumentSpec(
                symbol=occ.symbol,
                option_type=occ.option_type,
                strike_price=occ.strike,
                expiration_date=occ.expiration.isoformat(),
            ),
        ))
    for sym in sorted(symbols):
        labelled.append((f"stock:{sym}", InstrumentSpec(symbol=sym)))
    labelled.append(("index:VIX", InstrumentSpec(symbol="VIX", instrument_type="Index")))

    async with Session(
        provider_secret=env.tt_client_secret,
        refresh_token=env.tt_refresh_token,
    ) as session:
        # tasty-agent's get_instrument_details uses asyncio.gather with no
        # concurrency cap. 40+ simultaneous HTTPS calls to api.tastyworks.com
        # exhaust the connection pool and surface as httpx.ConnectTimeout.
        # Chunk the spec list so at most _CHUNK_SIZE requests run in parallel.
        specs = [spec for _, spec in labelled]
        details: list = []
        for i in range(0, len(specs), _CHUNK_SIZE):
            chunk = specs[i : i + _CHUNK_SIZE]
            details.extend(await get_instrument_details(session, chunk))
        streamer_symbols = [d.streamer_symbol for d in details]
        quotes = await stream_events(session, Quote, streamer_symbols, _QUOTE_TIMEOUT_SEC)
        metrics_list = await get_market_metrics(session, sorted(symbols))

    quote_by_label = {label: quote for (label, _), quote in zip(labelled, quotes)}
    metrics_by_sym = {m.symbol: m for m in metrics_list}
    return {"quote_by_label": quote_by_label, "metrics_by_sym": metrics_by_sym}


def _mid(bid: Any, ask: Any) -> float | None:
    if bid is None or ask is None:
        return None
    try:
        return (float(bid) + float(ask)) / 2.0
    except (TypeError, ValueError):
        return None


def _expected_move(underlying: float | None, ivx: float | None, dte: int) -> float | None:
    if underlying is None or ivx is None or underlying <= 0 or dte <= 0:
        return None
    # IVx as decimal (0.42 = 42%). Live metrics' implied_volatility_30_day
    # is already a decimal.
    return underlying * ivx * math.sqrt(dte / 365.0)


async def refresh_open_positions_async(
    env: Env, tracker: TrackerClient, today: date | None = None
) -> int:
    """Run the daily refresh. Returns the number of positions updated."""
    today = today or date.today()
    positions = tracker.read_open_positions()
    if not positions:
        log.info("Tracker refresh: no OPEN positions.")
        return 0

    parsed_positions: list[tuple[dict, Any]] = []
    for p in positions:
        occ_raw = str(p.get("OCC", "")).strip()
        try:
            parsed_positions.append((p, parse_occ(occ_raw)))
        except ValueError as e:
            log.warning("Tracker refresh: skipping unparseable OCC %r: %s", occ_raw, e)

    if not parsed_positions:
        log.info("Tracker refresh: no parseable OPEN positions.")
        return 0

    # Expired contracts can't be quoted — tastytrade drops them from the chain.
    # A single expired position would raise inside tasty-agent's asyncio.gather
    # and kill the whole batch, so filter them out up front.
    live, expired = [], []
    for p, occ in parsed_positions:
        (live if occ.expiration >= today else expired).append((p, occ))
    if expired:
        log.info(
            "Tracker refresh: skipping %d expired position(s) (Stan should mark CLOSED): %s",
            len(expired),
            ", ".join(p["OCC"] for p, _ in expired),
        )
    parsed_positions = live
    if not parsed_positions:
        log.info("Tracker refresh: no live OPEN positions after expiry filter.")
        return 0

    log.info("Tracker refresh: processing %d live open position(s)", len(parsed_positions))
    try:
        market = await _gather_market_data(env, parsed_positions)
    except Exception as e:
        log.exception("Tracker refresh: failed to gather market data: %s", e)
        return 0

    quote_by_label = market["quote_by_label"]
    metrics_by_sym = market["metrics_by_sym"]

    vix_quote = quote_by_label.get("index:VIX")
    vix_value: float | None = None
    if vix_quote is not None:
        # VIX as an index has a last/price field rather than bid/ask.
        vix_value = (
            _mid(getattr(vix_quote, "bid_price", None), getattr(vix_quote, "ask_price", None))
            or _to_float(getattr(vix_quote, "price", None))
        )

    updated = 0
    for position, occ in parsed_positions:
        # Replace the raw Expiration cell value (which on legacy rows is a
        # JS Date toString() like 'Thu Jun 18 2026 00:00:00 GMT-0700 ...')
        # with the parsed ISO date so the dedicated tab's static block heals.
        position["Expiration"] = occ.expiration.isoformat()
        try:
            row_updates, daily = _compute_row(
                position, occ, today, quote_by_label, metrics_by_sym, vix_value
            )
        except Exception as e:
            log.exception("Tracker refresh: compute failed for %s: %s", position.get("OCC"), e)
            continue

        if not row_updates:
            log.warning("Tracker refresh: no fresh data for %s — skipped", position.get("OCC"))
            continue

        try:
            tracker.update_main_row(position["OCC"], row_updates)
            tracker.append_daily_row(position["OCC"], position, daily)
            updated += 1
        except Exception as e:
            log.exception("Tracker refresh: write failed for %s: %s", position.get("OCC"), e)

    log.info("Tracker refresh: updated %d/%d position(s).", updated, len(parsed_positions))
    return updated


def _compute_row(
    position: dict,
    occ,
    today: date,
    quote_by_label: dict,
    metrics_by_sym: dict,
    vix_value: float | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    occ_str = position["OCC"]
    sym = occ.symbol

    opt_quote = quote_by_label.get(f"opt:{occ_str}")
    stock_quote = quote_by_label.get(f"stock:{sym}")
    metrics = metrics_by_sym.get(sym)

    if opt_quote is None:
        return {}, {}

    bid = _to_float(getattr(opt_quote, "bid_price", None))
    ask = _to_float(getattr(opt_quote, "ask_price", None))
    current_price = _mid(bid, ask)

    underlying = None
    if stock_quote is not None:
        underlying = _mid(
            getattr(stock_quote, "bid_price", None),
            getattr(stock_quote, "ask_price", None),
        ) or _to_float(getattr(stock_quote, "price", None))

    ivr = _to_float(getattr(metrics, "implied_volatility_index_rank", None)) if metrics else None
    if ivr is not None and ivr <= 1.5:
        # tastytrade returns iv_rank as 0-1; user-facing sheet shows percent.
        ivr = ivr * 100.0
    ivx = _to_float(getattr(metrics, "implied_volatility_30_day", None)) if metrics else None

    price_paid = _to_float(position.get("Price Paid"))
    pnl = (price_paid - current_price) if (price_paid is not None and current_price is not None) else None
    limit = (price_paid * 0.5) if price_paid is not None else None

    dte = max((occ.expiration - today).days, 0)
    expected_move = _expected_move(underlying, ivx, dte)

    row_updates: dict[str, Any] = {
        # Expiration is included so that legacy rows written with a JS
        # Date toString() (e.g. 'Thu Jun 18 2026 00:00:00 GMT-0700 ...')
        # get healed to a real ISO date on the next refresh.
        "Expiration": occ.expiration,
        "Current Price": current_price,
        "P&L": pnl,
        "IVR": ivr,
        "VIX": vix_value,
        "IVx": (ivx * 100.0) if ivx is not None else None,  # percent for display
        "Range": expected_move,
        "Limit": limit,
    }
    daily: dict[str, Any] = {
        "Date": today,
        "DTE": dte,
        "Underlying": underlying,
        "Bid": bid,
        "Ask": ask,
        "Current Price": current_price,
        "P&L": pnl,
        "IVR": ivr,
        "VIX": vix_value,
        "IVx": (ivx * 100.0) if ivx is not None else None,
        "Range": expected_move,
    }
    return row_updates, daily


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def refresh_open_positions(env: Env, tracker: TrackerClient) -> int:
    return asyncio.run(refresh_open_positions_async(env, tracker))
