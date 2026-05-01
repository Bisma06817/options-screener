"""Daily scan orchestrator.

The cloud entrypoint. Run with `python -m screener.main`. Reads the watchlist
and config from the Sheet, hands the screen off to the Claude agent (which
talks to tasty-agent over MCP, with Yahoo Finance as a fallback for earnings
dates and as the source for company names), then writes results back to the
Sheet.

Time gating: cron on the droplet fires this hourly. The script no-ops
unless current ET time is within `scan_window_minutes` of the configured
`scan_time_et`. After a successful scan, further fires the same ET day
also no-op so the brief's "once daily" requirement holds even when the
window spans multiple cron fires. `FORCE_RUN=1` bypasses both checks.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from .agent.screener_agent import run_screen
from .config import DEFAULTS, Env
from .data.yahoo import YahooClient
from .screen.filters import FilterParams
from .sink.sheets import SheetsClient

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
log = logging.getLogger("screener")


def _already_scanned_today(last_log: dict | None, now_et: datetime) -> bool:
    if not last_log or last_log.get("status") != "ok":
        return False
    try:
        ts_utc = datetime.fromisoformat(last_log["timestamp_utc"]).replace(tzinfo=UTC)
    except (ValueError, KeyError, TypeError):
        return False
    return ts_utc.astimezone(ET).date() == now_et.date()


def _within_scan_window(scan_time_et: str, window_minutes: int, now_et: datetime) -> bool:
    try:
        hh, mm = scan_time_et.split(":")
        target = now_et.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    except (ValueError, AttributeError):
        log.warning("Bad scan_time_et %r — falling back to default", scan_time_et)
        hh, mm = DEFAULTS.scan_time_et.split(":")
        target = now_et.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    delta_min = abs((now_et - target).total_seconds()) / 60.0
    return delta_min <= window_minutes


def _params_from_config(cfg: dict[str, str]) -> tuple[FilterParams, str, int]:
    def f(key: str, default: float) -> float:
        try:
            return float(cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    def i(key: str, default: int) -> int:
        try:
            return int(float(cfg.get(key, default)))
        except (TypeError, ValueError):
            return default

    fp = FilterParams(
        ivr_min=f("ivr_min", DEFAULTS.ivr_min),
        dte_min=i("dte_min", DEFAULTS.dte_min),
        dte_max=i("dte_max", DEFAULTS.dte_max),
        delta_min=f("delta_min", DEFAULTS.delta_min),
        delta_max=f("delta_max", DEFAULTS.delta_max),
    )
    scan_time_et = cfg.get("scan_time_et") or DEFAULTS.scan_time_et
    window = i("scan_window_minutes", DEFAULTS.scan_window_minutes)
    return fp, scan_time_et, window


def run() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    env = Env.load()
    sheets = SheetsClient(env.google_sa_json, env.spreadsheet_id)
    sheets.ensure_tabs()
    cfg = sheets.read_config()
    fp, scan_time_et, window = _params_from_config(cfg)

    now_et = datetime.now(ET)
    force = os.environ.get("FORCE_RUN") == "1"
    if not force and not _within_scan_window(scan_time_et, window, now_et):
        log.info(
            "Outside scan window (target %s ET, now %s) — skipping",
            scan_time_et, now_et.strftime("%H:%M"),
        )
        return 0

    if not force and _already_scanned_today(sheets.last_log_row(), now_et):
        log.info("Already scanned today — skipping")
        return 0

    watchlist = sheets.read_watchlist()
    log.info("Watchlist (%d): %s", len(watchlist), watchlist)
    if not watchlist:
        sheets.write_log("skipped", 0, 0, "empty watchlist")
        return 0

    yahoo = YahooClient()

    try:
        candidates = run_screen(env, watchlist, fp, yahoo)
    except Exception as e:
        log.exception("Scan failed")
        sheets.write_log("error", 0, len(watchlist), f"{type(e).__name__}: {e}")
        return 1

    sheets.write_results(candidates)
    sheets.write_log("ok", len(candidates), len(watchlist))
    log.info("Scan complete: %d candidates", len(candidates))
    return 0


if __name__ == "__main__":
    sys.exit(run())
