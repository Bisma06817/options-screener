"""Claude agent that drives the daily options screen via tasty-agent (MCP).

Architecture (per the client brief):

    Claude (Opus 4.7, adaptive thinking)
        │
        ├─ MCP tools  ──▶  tasty-agent  ──▶  tastytrade API (OAuth2)
        │                  (stdio MCP server, spawned per scan)
        │
        ├─ Custom tool: lookup_earnings_yahoo() ──▶ Yahoo Finance (fallback only)
        │
        └─ Custom tool: submit_candidate() ──▶ in-memory list returned to caller

Earnings dates come from tasty-agent's `get_market_metrics`
(`expected_report_date`). When that field is missing or null, Claude falls
back to `lookup_earnings_yahoo` (yfinance, no API key, retried with
exponential backoff) per the client brief.

The system prompt is prefix-cached (5-min TTL) so repeat scans within the
window pay ~0.1× input price on the cached portion.
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import date

from anthropic import AsyncAnthropic, beta_async_tool
from anthropic.lib.tools.mcp import async_mcp_tool
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from ..config import Env
from ..data.yahoo import YahooClient
from ..screen.filters import FilterParams

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an options-screening agent for selling cash-secured / naked short puts.

Your task: from a watchlist of stocks, return all puts that pass ALL four filters:
1. The symbol's IVR (Implied Volatility Rank) is >= the threshold.
2. The put expires within the configured DTE window (calendar days, today inclusive of lower bound).
3. The put's absolute delta is within the configured delta band.
4. There is no earnings announcement on or between today and the put's expiry (inclusive).

Output: for each candidate that passes every filter, call `submit_candidate` exactly once
with the requested fields. After processing the full watchlist, respond with a one-sentence summary.

Process — be efficient and avoid unnecessary tool calls:

Step 1. Call the tasty-agent `get_market_metrics` tool ONCE for the entire watchlist.
        From its response, capture per symbol:
          - IVR (Implied Volatility Rank)
          - IVx (implied-volatility index)
          - `expected_report_date` — the next scheduled earnings date. May be null
            or missing for some symbols.

Step 2. Pre-filter symbols: keep only those whose IVR meets the threshold.

Step 3. For each surviving symbol, in order:
   3a. Determine the symbol's earnings date:
       - If `expected_report_date` from `get_market_metrics` is present, use it.
       - If it is null/missing, call `lookup_earnings_yahoo(symbol)` ONCE for that
         symbol. The tool returns ISO YYYY-MM-DD or the literal string 'none'.
       - Treat 'none' (from either source) as "no upcoming earnings".
       If the earnings date falls inside the DTE window (today..max-DTE-from-today
       inclusive), SKIP this symbol entirely. Do not fetch option data for it.
   3b. Use the appropriate tasty-agent tool to enumerate puts whose expiry is in the DTE window.
       Restrict yourself to OTM puts (strike < spot) within roughly 25% of the underlying;
       far-OTM puts will fail the delta filter.
   3c. Batch-call `get_quotes` and `get_greeks` for the candidate put symbols (one batch per
       symbol if the tool supports lists).
   3d. For each put with |delta| in the configured band, AND with bid > 0 AND ask > 0,
       call `submit_candidate` with the fields below. Pass the symbol's earnings
       date (from step 3a) as `earnings_date`, or null if neither source returned one.

Field rules for `submit_candidate`:
- `expiry` and `earnings_date` are ISO YYYY-MM-DD; `earnings_date` may be null.
- `put_price` is the bid-ask mid: (bid + ask) / 2.
- `pop_pct` is the standard short-put proxy: (1 - |delta|) * 100, in percent (0-100).
- `expected_move` = underlying_price * IVx * sqrt(DTE / 365). IVx is decimal (0.42 = 42%).
  If you do not have a usable IVx, pass null.
- `delta` is signed (negative for puts).

Hard rules:
- NEVER call `submit_candidate` for a contract that fails any filter.
- NEVER call `place_order` or any account-mutating tool. This is a read-only screening run.
- If a tool fails for a symbol, log it (in your reasoning) and skip that symbol — do not retry forever.
"""


def _user_prompt(watchlist: list[str], fp: FilterParams, today: date) -> str:
    return (
        f"Today's date: {today.isoformat()}\n"
        f"Watchlist ({len(watchlist)} symbols): {', '.join(watchlist)}\n\n"
        f"Filter thresholds:\n"
        f"  IVR >= {fp.ivr_min}\n"
        f"  DTE between {fp.dte_min} and {fp.dte_max}\n"
        f"  |delta| between {fp.delta_min} and {fp.delta_max}\n\n"
        "Run the screen now and submit every qualifying candidate."
    )


def _tasty_agent_env(env: Env) -> dict[str, str]:
    return {
        "TASTYTRADE_CLIENT_SECRET": env.tt_client_secret,
        "TASTYTRADE_REFRESH_TOKEN": env.tt_refresh_token,
        "TASTYTRADE_ACCOUNT_ID": env.tt_account_id,
    }


async def run_screen_async(
    env: Env,
    watchlist: list[str],
    fp: FilterParams,
    yahoo_client: YahooClient,
) -> list[dict]:
    today = date.today()
    candidates: list[dict] = []

    @beta_async_tool
    async def submit_candidate(
        symbol: str,
        strike: float,
        put_price: float,
        dte: int,
        pop_pct: float,
        ivr: float,
        delta: float,
        expiry: str,
        bid: float,
        ask: float,
        underlying_price: float,
        earnings_date: str | None = None,
        expected_move: float | None = None,
    ) -> str:
        """Record a put that passed every screening filter.

        Call this exactly once per qualifying contract. All numeric fields are
        plain floats / ints; expiry and earnings_date are ISO YYYY-MM-DD strings.
        """
        try:
            exp_d = date.fromisoformat(expiry)
        except ValueError:
            return f"rejected: bad expiry format {expiry!r}, expected YYYY-MM-DD"
        ed = None
        if earnings_date:
            try:
                ed = date.fromisoformat(earnings_date)
            except ValueError:
                return f"rejected: bad earnings_date {earnings_date!r}"
        sym_u = symbol.upper()
        candidates.append({
            "scan_date": today,
            "symbol": sym_u,
            "company": yahoo_client.company_name(sym_u),
            "strike": strike,
            "put_price": put_price,
            "dte": dte,
            "pop_pct": pop_pct,
            "ivr": ivr,
            "delta": delta,
            "expiry": exp_d,
            "p50_pct": None,
            "bid": bid,
            "ask": ask,
            "spread": ask - bid,
            "underlying_price": underlying_price,
            "earnings_date": ed,
            "expected_move": expected_move,
        })
        return f"recorded {symbol} {expiry} {strike}P (delta {delta:.3f})"

    @beta_async_tool
    async def lookup_earnings_yahoo(symbol: str) -> str:
        """Fallback earnings lookup via Yahoo Finance.

        Use this ONLY when tasty-agent's `get_market_metrics` did not return
        an `expected_report_date` for the symbol. Returns the next scheduled
        earnings date as ISO YYYY-MM-DD, or the literal string 'none' if no
        upcoming earnings are known. The lookup is retried with exponential
        backoff on transient errors; if it ultimately fails, 'none' is returned
        so the scan keeps moving.
        """
        try:
            d = await asyncio.to_thread(
                yahoo_client.next_earnings_date, symbol.upper()
            )
        except Exception as e:
            log.warning("Yahoo earnings lookup raised for %s: %s", symbol, e)
            return "none"
        return d.isoformat() if d else "none"

    server_params = StdioServerParameters(
        command="tasty-agent",
        args=[],
        env=_tasty_agent_env(env),
    )

    client = AsyncAnthropic(api_key=env.anthropic_api_key)

    candidates.sort(key=lambda c: -(c.get("ivr") or 0.0))  # safety; agent should rank too

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as mcp_client:
            await mcp_client.initialize()
            mcp_tools_result = await mcp_client.list_tools()
            allowed = _safe_mcp_tools(mcp_tools_result.tools)
            log.info("MCP tools available to agent: %s", [t.name for t in allowed])
            log.debug("MCP tool schemas: %s", [t.model_dump() for t in allowed])
            mcp_tool_defs = [async_mcp_tool(t, mcp_client) for t in allowed]

            user_prompt = _user_prompt(watchlist, fp, today)
            log.debug("agent user prompt: %s", user_prompt)

            runner = client.beta.messages.tool_runner(
                model="claude-opus-4-7",
                max_tokens=16000,
                thinking={"type": "adaptive"},
                output_config={"effort": "high"},
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_prompt}],
                tools=[*mcp_tool_defs, submit_candidate, lookup_earnings_yahoo],
            )

            async for message in runner:
                u = getattr(message, "usage", None)
                if u:
                    log.info(
                        "agent step: in=%s cache_read=%s cache_write=%s out=%s stop=%s",
                        getattr(u, "input_tokens", "?"),
                        getattr(u, "cache_read_input_tokens", "?"),
                        getattr(u, "cache_creation_input_tokens", "?"),
                        getattr(u, "output_tokens", "?"),
                        getattr(message, "stop_reason", "?"),
                    )
                # Full message body (content blocks, tool calls, tool results) at
                # DEBUG. Set LOG_LEVEL=DEBUG during initial testing per the brief.
                try:
                    log.debug("agent message: %s", message.model_dump())
                except Exception:
                    log.debug("agent message (repr): %r", message)

    candidates.sort(key=lambda c: -(c.get("ivr") or 0.0))
    return candidates


_BLOCKED_TOOL_NAMES = {
    # Defense-in-depth: even though the system prompt forbids it, refuse to
    # expose any account-mutating tasty-agent tools to Claude.
    "place_order", "replace_order", "cancel_order",
}


def _safe_mcp_tools(tools):
    return [t for t in tools if t.name not in _BLOCKED_TOOL_NAMES]


def _expected_move_safety(spot: float, ivx: float | None, dte: int) -> float | None:
    """Reference helper — Claude is expected to compute this itself per the
    system prompt, but we keep it here for unit testing.
    """
    if ivx is None or spot <= 0 or dte <= 0:
        return None
    return spot * ivx * math.sqrt(dte / 365.0)


def run_screen(
    env: Env,
    watchlist: list[str],
    fp: FilterParams,
    yahoo_client: YahooClient,
) -> list[dict]:
    return asyncio.run(run_screen_async(env, watchlist, fp, yahoo_client))
