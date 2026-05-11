"""Google Sheets I/O.

Tabs (created on first run if missing):
  - Watchlist : column A header "Symbol", rows of tickers (you edit)
  - Config    : key/value rows (you edit; missing keys fall back to defaults)
  - Latest    : today's results, fully overwritten each scan
  - History   : every scan's results appended
  - Logs      : one row per scan run with status / error / row count

All Sheets reads/writes are wrapped with tenacity exponential backoff so a
transient gspread / Google API error doesn't kill a scan. All numeric writes
go through `_round` so the sheet stays readable.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import gspread
from google.oauth2.service_account import Credentials
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..config import DEFAULTS

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Standard backoff: 3 attempts, exponential 1-8s. Applied to every Sheets
# call so 429 / 5xx / transient network blips are retried before we surface
# the failure to the Logs tab.
_RETRY = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=1, max=8),
    retry=retry_if_exception_type(Exception),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=True,
)

OUTPUT_HEADERS = [
    "Scan Date", "Symbol", "Company", "OCC Symbol", "Strike", "Put Price",
    "DTE", "POP%", "IVR%", "Delta", "Expiry Date", "P50%", "Bid", "Ask",
    "Spread", "Underlying Price", "Earnings Date", "Expected Move", "Track",
]

# 1-based column indices of $-denominated fields in OUTPUT_HEADERS, used to
# apply a 2-decimal numberFormat so the Sheet displays "12.50" instead of
# "12.5". Update if you reorder OUTPUT_HEADERS.
_CURRENCY_COL_INDICES = [5, 6, 13, 14, 15, 16, 18]  # Strike, Put Price, Bid, Ask, Spread, Underlying, Expected Move

# 1-based column index of the "Track" checkbox column. When ticked by the
# user in the Latest tab, an Apps Script onEdit trigger copies that row's
# data into the tracker sheet to start daily price tracking on the option.
_TRACK_COL_INDEX = 19

DEFAULT_WATCHLIST = [
    "MU", "SNOW", "ORCL", "BIDU", "CRM", "AVGO", "ADBE", "BABA", "MRVL",
    "LULU", "VST", "NVDA", "META", "MSFT", "TSLA",
]

DEFAULT_CONFIG = {
    "scan_time_et": DEFAULTS.scan_time_et,
    "scan_window_minutes": DEFAULTS.scan_window_minutes,
    "ivr_min": DEFAULTS.ivr_min,
    "dte_min": DEFAULTS.dte_min,
    "dte_max": DEFAULTS.dte_max,
    "delta_min": DEFAULTS.delta_min,
    "delta_max": DEFAULTS.delta_max,
}

LOG_HEADERS = ["Timestamp UTC", "Status", "Rows", "Symbols Scanned", "Error"]


class SheetsClient:
    def __init__(self, service_account_json: str, spreadsheet_id: str):
        info = json.loads(service_account_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        self._gc = gspread.authorize(creds)
        self._sh = self._gc.open_by_key(spreadsheet_id)

    # ---------- bootstrap ----------

    @_RETRY
    def ensure_tabs(self) -> None:
        existing = {ws.title for ws in self._sh.worksheets()}
        if "Watchlist" not in existing:
            ws = self._sh.add_worksheet("Watchlist", rows=100, cols=2)
            ws.update("A1", [["Symbol"], *[[s] for s in DEFAULT_WATCHLIST]])
        if "Config" not in existing:
            ws = self._sh.add_worksheet("Config", rows=50, cols=2)
            rows = [["Key", "Value"]] + [[k, str(v)] for k, v in DEFAULT_CONFIG.items()]
            ws.update("A1", rows)
        if "Latest" not in existing:
            self._sh.add_worksheet("Latest", rows=200, cols=len(OUTPUT_HEADERS))
        if "History" not in existing:
            self._sh.add_worksheet("History", rows=2000, cols=len(OUTPUT_HEADERS))
        if "Logs" not in existing:
            self._sh.add_worksheet("Logs", rows=500, cols=len(LOG_HEADERS))
        # Sync headers on every run so column additions/reorderings self-heal
        # on existing tabs (otherwise History row 1 stays frozen at first-run layout).
        self._sync_header("Latest", OUTPUT_HEADERS)
        self._sync_header("History", OUTPUT_HEADERS)
        self._sync_header("Logs", LOG_HEADERS)

    def _sync_header(self, tab: str, headers: list[str]) -> None:
        ws = self._sh.worksheet(tab)
        _ensure_cols(ws, len(headers))
        current = ws.row_values(1)
        if current[: len(headers)] != headers:
            ws.update("A1", [headers])

    # ---------- reads ----------

    @_RETRY
    def read_watchlist(self) -> list[str]:
        ws = self._sh.worksheet("Watchlist")
        col = ws.col_values(1)
        # drop header, drop blanks, normalize
        return [c.strip().upper() for c in col[1:] if c and c.strip()]

    @_RETRY
    def read_config(self) -> dict[str, str]:
        ws = self._sh.worksheet("Config")
        rows = ws.get_all_values()
        out = {}
        for r in rows[1:]:
            if len(r) >= 2 and r[0].strip():
                out[r[0].strip()] = r[1].strip()
        return out

    @_RETRY
    def last_log_row(self) -> dict[str, str] | None:
        ws = self._sh.worksheet("Logs")
        rows = ws.get_all_values()
        if len(rows) < 2:
            return None
        last = list(rows[-1]) + [""] * 5
        return {
            "timestamp_utc": last[0],
            "status": last[1],
            "rows": last[2],
            "symbols_scanned": last[3],
            "error": last[4],
        }

    # ---------- writes ----------

    @_RETRY
    def write_results(self, candidates: list[dict]) -> None:
        rows = [_to_row(c) for c in candidates]
        latest = self._sh.worksheet("Latest")
        _ensure_cols(latest, len(OUTPUT_HEADERS))
        latest.clear()
        latest.update("A1", [OUTPUT_HEADERS] + rows, value_input_option="USER_ENTERED")
        _apply_currency_format(latest)
        self._apply_track_checkbox(latest, num_data_rows=len(rows))
        if rows:
            history = self._sh.worksheet("History")
            _ensure_cols(history, len(OUTPUT_HEADERS))
            history.append_rows(rows, value_input_option="USER_ENTERED")
            _apply_currency_format(history)

    def _apply_track_checkbox(self, ws, num_data_rows: int) -> None:
        """Set data validation rule (BOOLEAN / checkbox UI) on the Track
        column. Without this, the cells render as the literal text "FALSE"
        instead of as tickable checkboxes.
        """
        if num_data_rows <= 0:
            return
        try:
            self._sh.batch_update({
                "requests": [{
                    "setDataValidation": {
                        "range": {
                            "sheetId": ws.id,
                            "startRowIndex": 1,  # skip header
                            "endRowIndex": 1 + num_data_rows,
                            "startColumnIndex": _TRACK_COL_INDEX - 1,
                            "endColumnIndex": _TRACK_COL_INDEX,
                        },
                        "rule": {
                            "condition": {"type": "BOOLEAN"},
                            "showCustomUi": True,
                            "strict": True,
                        },
                    }
                }]
            })
        except Exception as e:
            log.warning("Could not apply Track checkbox validation: %s", e)

    @_RETRY
    def write_log(self, status: str, rows: int, symbols_scanned: int, error: str = "") -> None:
        ws = self._sh.worksheet("Logs")
        ws.append_row(
            [datetime.utcnow().isoformat(timespec="seconds"), status, rows, symbols_scanned, error],
            value_input_option="USER_ENTERED",
        )


def _round(v: Any, places: int = 4) -> Any:
    if v is None:
        return ""
    if isinstance(v, float):
        return round(v, places)
    return v


def _ensure_cols(ws, min_cols: int) -> None:
    # Older Latest/History tabs were created with fewer columns; expand the
    # grid so update() / append_rows() don't fail with "out of bounds".
    if ws.col_count < min_cols:
        ws.resize(rows=ws.row_count, cols=min_cols)


def _col_letter(n: int) -> str:
    # 1 -> A, 26 -> Z, 27 -> AA. Sheets columns are 1-indexed.
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _apply_currency_format(ws) -> None:
    # Apply "0.00" two-decimal formatting to every $-denominated column so
    # the Sheet always shows trailing zeros (12.50 instead of 12.5).
    fmt = {"numberFormat": {"type": "NUMBER", "pattern": "0.00"}}
    for col in _CURRENCY_COL_INDICES:
        letter = _col_letter(col)
        try:
            ws.format(f"{letter}2:{letter}", fmt)
        except Exception as e:
            log.warning("Could not apply currency format to %s: %s", letter, e)


def _to_row(c: dict) -> list[Any]:
    return [
        c.get("scan_date").isoformat() if c.get("scan_date") else "",
        c.get("symbol", ""),
        c.get("company", ""),
        c.get("occ_symbol", ""),
        _round(c.get("strike"), 2),
        _round(c.get("put_price"), 2),
        c.get("dte", ""),
        _round(c.get("pop_pct"), 1),
        _round(c.get("ivr"), 1),
        _round(c.get("delta"), 4),
        c.get("expiry").isoformat() if c.get("expiry") else "",
        _round(c.get("p50_pct"), 1),
        _round(c.get("bid"), 2),
        _round(c.get("ask"), 2),
        _round(c.get("spread"), 2),
        _round(c.get("underlying_price"), 2),
        c.get("earnings_date").isoformat() if c.get("earnings_date") else "",
        _round(c.get("expected_move"), 2),
        False,  # Track checkbox — Apps Script onEdit fires when user ticks this
    ]
