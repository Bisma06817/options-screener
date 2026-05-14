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

    # Tab names to try (in order) before falling back to header scan.
    # Stan's workbook uses "Tab" as the main tab name; the others are
    # common fallbacks.
    _CANDIDATE_TAB_NAMES = ("Tab", "Positions", "Tracker", "Open Positions",
                            "Tasty Trade Open Position 2026")
    # Cap how many tabs we header-scan before giving up. Stan's workbook
    # has ~50 per-option historical tabs; scanning every one hits Sheets
    # rate limits.
    _MAX_HEADER_SCAN = 10
    # Stan's main tab has a title row at row 1 and a blank at row 2; the
    # actual OCC header is on row 3. Scan a few rows down to find it.
    _MAX_HEADER_ROW = 5

    def _find_header_row(self, ws) -> int | None:
        """Return the 1-based row number containing an 'OCC' header, or None."""
        for row_num in range(1, self._MAX_HEADER_ROW + 1):
            try:
                row = ws.row_values(row_num)
            except Exception:
                continue
            if any("OCC" in str(h).strip().upper() for h in row):
                return row_num
        return None

    def _main_tab(self) -> tuple[gspread.Worksheet | None, int | None]:
        """Find the main tracker tab + its header row.

        Returns (None, None) if no suitable tab exists yet — caller treats
        this as "no positions to track" rather than an error. The Apps
        Script tick handler creates the tab on first checkbox tick.
        """
        if self._main_tab_cache is not None:
            return self._main_tab_cache

        # Try common tab names first (cheap, ~1 API call per try).
        for name in self._CANDIDATE_TAB_NAMES:
            try:
                ws = self._sh.worksheet(name)
            except gspread.WorksheetNotFound:
                continue
            header_row = self._find_header_row(ws)
            if header_row is not None:
                self._main_tab_cache = (ws, header_row)
                log.info("Tracker main tab (named %r, header row %d)", ws.title, header_row)
                return self._main_tab_cache

        # Fall back to scanning the first few sheets for an OCC-style
        # header. Permissive match — any cell containing 'OCC' counts.
        for ws in self._sh.worksheets()[: self._MAX_HEADER_SCAN]:
            header_row = self._find_header_row(ws)
            if header_row is not None:
                self._main_tab_cache = (ws, header_row)
                log.info("Tracker main tab (scan %r, header row %d)", ws.title, header_row)
                return self._main_tab_cache

        log.info(
            "Tracker main tab not found (tried %s and scanned first %d sheets, "
            "checking rows 1-%d for an OCC header). Refresh will skip.",
            self._CANDIDATE_TAB_NAMES, self._MAX_HEADER_SCAN, self._MAX_HEADER_ROW,
        )
        return (None, None)

    @_RETRY
    def read_open_positions(self) -> list[dict]:
        """Return every row on the main tab where Status == OPEN.

        Returns an empty list (no exception) if the main tab doesn't yet
        exist — the Apps Script tick handler will create it on first use.
        """
        ws, header_row = self._main_tab()
        if ws is None or header_row is None:
            return []
        try:
            records = ws.get_all_records(head=header_row)
        except Exception as e:
            log.warning("Tracker main tab %r could not be parsed: %s", ws.title, e)
            return []
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
        ws, header_row = self._main_tab()
        if ws is None or header_row is None:
            log.warning("update_main_row: no main tab exists — skipping %s", occ)
            return
        try:
            records = ws.get_all_records(head=header_row)
        except Exception as e:
            log.warning("update_main_row: cannot parse main tab: %s", e)
            return
        target_row = None
        for i, r in enumerate(records):
            if str(r.get("OCC", "")).strip() == occ:
                # +1 to step past header, then +header_row to account for
                # the rows before the header. Result is the 1-based row
                # number of this record in the sheet.
                target_row = header_row + 1 + i
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
        """Create the per-option tab if missing; always (re)write the static
        block + daily-row header so existing tabs with stale or malformed
        static info self-heal on the next refresh.
        """
        try:
            ws = self._sh.worksheet(occ)
        except gspread.WorksheetNotFound:
            ws = self._sh.add_worksheet(
                occ, rows=500, cols=max(len(DAILY_HEADERS), len(STATIC_HEADERS))
            )
            log.info("Created dedicated tracker tab: %s", occ)

        static_values = [_serialize(position.get(h, "")) for h in STATIC_HEADERS]
        ws.update("A1", [STATIC_HEADERS, static_values], value_input_option="USER_ENTERED")
        # Row 3 stays blank as a visual separator.
        ws.update(f"A{_DAILY_HEADER_ROW}", [DAILY_HEADERS], value_input_option="USER_ENTERED")
        return ws

    @_RETRY
    def append_daily_row(
        self, occ: str, position: dict[str, Any], daily: dict[str, Any]
    ) -> None:
        """Upsert today's daily row on this option's dedicated tab.

        If the last data row's Date equals today's daily['Date'], overwrite
        that row (so the Apps Script's partial day-1 row gets healed on the
        next cron). Otherwise append a new row.

        `daily` keys must be column header names from DAILY_HEADERS.
        Missing keys are written as empty string.
        """
        ws = self.ensure_dedicated_tab(occ, position)
        row = [_serialize(daily.get(h, "")) for h in DAILY_HEADERS]
        today_iso = _serialize(daily.get("Date", ""))

        # Read the Date column unformatted so we get either a Sheets serial
        # (number, when the cell is a real date) or the raw string (when it
        # was written as text). Locale display format then doesn't matter.
        date_col = ws.col_values(1, value_render_option="UNFORMATTED_VALUE")
        last_row = len(date_col)
        same_day = (
            last_row > _DAILY_HEADER_ROW
            and _is_same_iso_date(date_col[last_row - 1], today_iso)
        )
        if same_day:
            target_a1 = f"A{last_row}:{_col_letter(len(DAILY_HEADERS))}{last_row}"
            ws.update(target_a1, [row], value_input_option="USER_ENTERED")
        else:
            ws.append_row(row, value_input_option="USER_ENTERED")


def _serialize(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float):
        return round(v, 4)
    return v


# Sheets stores dates as days since 1899-12-30 (Lotus 1-2-3 compat).
_SHEETS_EPOCH = date(1899, 12, 30)


def _is_same_iso_date(cell_value: Any, iso: str) -> bool:
    """True if cell_value (unformatted) represents the same calendar date
    as the iso string (e.g. '2026-05-13').
    """
    if not iso:
        return False
    try:
        target = date.fromisoformat(str(iso).strip())
    except ValueError:
        return False
    if isinstance(cell_value, (int, float)):
        # Sheets serial: integer days since 1899-12-30.
        try:
            target_serial = (target - _SHEETS_EPOCH).days
            return int(cell_value) == target_serial
        except (ValueError, OverflowError):
            return False
    s = str(cell_value).strip()
    if not s:
        return False
    try:
        return date.fromisoformat(s) == target
    except ValueError:
        return False


def _col_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s
