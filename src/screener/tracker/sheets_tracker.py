"""Tracker sheet I/O.

Client-owned Google Sheet `1nBrY1N...` separate from the screener's output
sheet. Schema:

  Main tab (row-per-position, columns must match the existing sheet
  Stan built — order is fixed):
    OCC | Symbol | Company | Strike | Expiration | Purchase Date |
    Price Paid | Current Price | P&L | Status | IVR | VIX | IVx |
    Range | Limit

  Per-option tabs (one tab per ticked option, named with the OCC string):
    Rows 1-2 : static-info header + values (carried from the main row)
    Row 3    : blank separator
    Row 4    : daily-row headers
    Row 5+   : one row appended per scan with the day's price + IV data

All gspread calls are wrapped with the same tenacity backoff as the
screener output client so transient API blips don't kill the refresh.
"""
from __future__ import annotations

import json
import logging
from datetime import date
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

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

_RETRY = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=1, max=8),
    retry=retry_if_exception_type(Exception),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=True,
)

# Main-tab headers — MUST match the live tracker sheet column order.
# Limit is the 15th column; if Stan's sheet currently only has 14, the
# first refresh that writes a Limit value will extend the grid.
MAIN_HEADERS = [
    "OCC", "Symbol", "Company", "Strike", "Expiration",
    "Purchase Date", "Price Paid", "Current Price", "P&L", "Status",
    "IVR", "VIX", "IVx", "Range", "Limit",
]

# Per-option tab daily row schema.
DAILY_HEADERS = [
    "Date", "DTE", "Underlying", "Bid", "Ask", "Current Price",
    "P&L", "IVR", "VIX", "IVx", "Range",
]

# Per-option tab static-info block (rows 1-2).
STATIC_HEADERS = [
    "OCC", "Symbol", "Company", "Strike", "Expiration",
    "Purchase Date", "Price Paid", "Limit",
]

# 1-based row where the daily-row header is written (rows 1-2 = static,
# row 3 = blank separator).
_DAILY_HEADER_ROW = 4


class TrackerClient:
    def __init__(self, service_account_json: str, spreadsheet_id: str):
        info = json.loads(service_account_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        self._gc = gspread.authorize(creds)
        self._sh = self._gc.open_by_key(spreadsheet_id)
        self._main_tab_cache: gspread.Worksheet | None = None

    def _main_tab(self) -> gspread.Worksheet:
        if self._main_tab_cache is None:
            # The main tracker tab is identified by having "OCC" in row 1.
            # Stan's workbook has a "Summary" tab at index 0 with merged
            # cells / blank header — using positional lookup hits that and
            # crashes get_all_records(). Scanning for the OCC header is
            # robust to any tab order.
            for ws in self._sh.worksheets():
                try:
                    row1 = ws.row_values(1)
                except Exception:
                    continue
                if any(str(h).strip().upper() == "OCC" for h in row1):
                    self._main_tab_cache = ws
                    log.info("Tracker main tab: %r", ws.title)
                    break
            if self._main_tab_cache is None:
                raise RuntimeError(
                    "Could not find tracker main tab — no worksheet has 'OCC' header in row 1"
                )
        return self._main_tab_cache

    @_RETRY
    def read_open_positions(self) -> list[dict]:
        """Return every row on the main tab where Status == OPEN."""
        ws = self._main_tab()
        records = ws.get_all_records(head=1)
        return [
            r for r in records
            if str(r.get("Status", "")).strip().upper() == "OPEN"
            and str(r.get("OCC", "")).strip()
        ]

    @_RETRY
    def update_main_row(self, occ: str, updates: dict[str, Any]) -> None:
        """Update specific columns on the main row matching the given OCC.

        `updates` keys must be column header names from MAIN_HEADERS.
        """
        ws = self._main_tab()
        records = ws.get_all_records(head=1)
        target_row = None
        for i, r in enumerate(records):
            if str(r.get("OCC", "")).strip() == occ:
                target_row = i + 2  # +1 header, +1 1-based
                break
        if target_row is None:
            log.warning("update_main_row: OCC %s not found on main tab", occ)
            return

        # Build a list of (a1, value) cell updates so we batch into one API call.
        cell_updates: list[dict[str, Any]] = []
        for col_name, value in updates.items():
            if col_name not in MAIN_HEADERS:
                log.warning("update_main_row: unknown column %r — skipped", col_name)
                continue
            col_idx = MAIN_HEADERS.index(col_name) + 1  # 1-based
            cell_updates.append({
                "range": f"{_col_letter(col_idx)}{target_row}",
                "values": [[_serialize(value)]],
            })
        if cell_updates:
            ws.batch_update(cell_updates, value_input_option="USER_ENTERED")

    @_RETRY
    def ensure_dedicated_tab(
        self, occ: str, position: dict[str, Any]
    ) -> gspread.Worksheet:
        """Create the per-option tab if missing, populate static-info block."""
        try:
            ws = self._sh.worksheet(occ)
            return ws
        except gspread.WorksheetNotFound:
            pass

        ws = self._sh.add_worksheet(
            occ, rows=500, cols=max(len(DAILY_HEADERS), len(STATIC_HEADERS))
        )
        # Static info: rows 1-2
        static_values = [_serialize(position.get(h, "")) for h in STATIC_HEADERS]
        ws.update("A1", [STATIC_HEADERS, static_values], value_input_option="USER_ENTERED")
        # Row 3 stays blank as a visual separator.
        # Daily-row headers on row 4.
        ws.update(f"A{_DAILY_HEADER_ROW}", [DAILY_HEADERS], value_input_option="USER_ENTERED")
        log.info("Created dedicated tracker tab: %s", occ)
        return ws

    @_RETRY
    def append_daily_row(
        self, occ: str, position: dict[str, Any], daily: dict[str, Any]
    ) -> None:
        """Append one new daily row to this option's dedicated tab.

        `daily` keys must be column header names from DAILY_HEADERS.
        Missing keys are written as empty string.
        """
        ws = self.ensure_dedicated_tab(occ, position)
        row = [_serialize(daily.get(h, "")) for h in DAILY_HEADERS]
        ws.append_row(row, value_input_option="USER_ENTERED")


def _serialize(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float):
        return round(v, 4)
    return v


def _col_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s
