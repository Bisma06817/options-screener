"""Tracker sheet I/O.

Client-owned Google Sheet `1nBrY1N...` separate from the screener's output
sheet. Schema:

  Main tab (row-per-position, columns must match the existing sheet
  Stan built — order is fixed):
    OCC | Symbol | Company | Strike | Expiration | Purchase Date |
    Price Paid | Current Price | P&L | Status | IVR | VIX | IVx |
    Range | Limit

  Per-option tabs (one tab per ticked option, named with the OCC string)
  follow Stan's legacy layout exactly:
    Row 1: Position: <Company> (<SYMBOL>) - <Strike> Put
    Row 2: Symbol: <SYM>  Name: <Company>  Strike: <strike>
           Price Paid: <price>  IVx: <ivx>%  Range: <range>
    Row 3: Expiration: <expiry>  Quantity: 1  Direction: Short
           Purchase Date: <date>  VIX: <vix>
    Row 4: (blank)
    Row 5: Date | OCC | Expiration | DTE | Share Price | Strike |
           Difference | Option Price | P&L | Range | Limit
    Row 6+: one row per scan (Date in col A, upsert by date).

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

# Per-option tab daily-data column headers — matches Stan's legacy layout.
PER_OPTION_DAILY_HEADERS = [
    "Date", "OCC", "Expiration", "DTE", "Share Price", "Strike",
    "Difference", "Option Price", "P&L", "Range", "Limit",
]

# Layout positions on the per-option tab (1-based row/col numbers).
_HEADER_ROW = 5         # daily column headers live on row 5
_DATA_START_ROW = 6     # daily rows start at row 6
_DATE_COL = 1           # Date is column A in the daily table

# Cell colours matching Stan's existing per-option tabs.
# Dark blue for title + column-header row, medium blue for label cells,
# white bold text. Values are gspread's 0-1 RGB floats.
_DARK_BLUE = {"red": 31 / 255, "green": 56 / 255, "blue": 100 / 255}
_MEDIUM_BLUE = {"red": 46 / 255, "green": 117 / 255, "blue": 182 / 255}
_WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}

_TITLE_FMT = {
    "backgroundColor": _DARK_BLUE,
    "textFormat": {"foregroundColor": _WHITE, "bold": True},
    "horizontalAlignment": "LEFT",
}
_LABEL_FMT = {
    "backgroundColor": _MEDIUM_BLUE,
    "textFormat": {"foregroundColor": _WHITE, "bold": True},
}
_HEADER_FMT = _TITLE_FMT


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
    def update_main_row(
        self,
        occ: str,
        updates: dict[str, Any],
        dedicated_tab_gid: int | None = None,
    ) -> None:
        """Update specific columns on the main row matching the given OCC.

        `updates` keys must be column header names from MAIN_HEADERS.

        When `dedicated_tab_gid` is given, the OCC cell is (re)written as an
        in-sheet HYPERLINK formula pointing at that option's dedicated tab,
        so Stan can click straight from the main listing to the per-option
        sheet. The cell still *displays* the OCC string, so the OCC join key
        keeps reading back correctly via get_all_records.
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
        if dedicated_tab_gid is not None:
            occ_col = MAIN_HEADERS.index("OCC") + 1  # 1-based
            cell_updates.append({
                "range": f"{_col_letter(occ_col)}{target_row}",
                "values": [[_occ_hyperlink(occ, dedicated_tab_gid)]],
            })
        if cell_updates:
            ws.batch_update(cell_updates, value_input_option="USER_ENTERED")

    @_RETRY
    def ensure_dedicated_tab(
        self, occ: str, position: dict[str, Any]
    ) -> gspread.Worksheet:
        """Return the per-option tab, creating or migrating it as needed.

        Layout target: rows 1-3 hold Stan's static info block, row 4 is
        blank, row 5 is PER_OPTION_DAILY_HEADERS, row 6+ is daily data.

        Three cases:
        1. Tab doesn't exist: create with the static block and headers
           ready for the first append_daily_row.
        2. Tab exists with row 1 starting "Position:" — already on the
           target layout (Stan's legacy tabs and our newly-created tabs
           both look like this). Leave it alone, only daily-row writes.
        3. Tab exists with row 1 starting "OCC" — our previous flat-table
           layout (fe653be / 1b35d64). Migrate: salvage the existing daily
           rows, re-shape into the new 11-column daily schema, and write
           the new static block on top.
        4. Anything else: log and leave alone, never destroy unknown data.
        """
        try:
            ws = self._sh.worksheet(occ)
        except gspread.WorksheetNotFound:
            ws = self._sh.add_worksheet(
                occ, rows=500, cols=len(PER_OPTION_DAILY_HEADERS)
            )
            self._write_static_and_headers(ws, position)
            log.info("Created dedicated tracker tab: %s", occ)
            return ws

        first = ""
        try:
            row1 = ws.row_values(1)
            first = (row1[0] if row1 else "").strip()
        except Exception:
            pass

        if first.startswith("Position:"):
            return ws

        if first == "OCC":
            self._migrate_flat_layout(ws, occ, position)
            return ws

        log.warning(
            "Tracker tab %s has unrecognised layout (row 1 starts %r); leaving as-is",
            occ, first[:40],
        )
        return ws

    @_RETRY
    def append_daily_row(
        self,
        occ: str,
        position: dict[str, Any],
        daily: dict[str, Any],
        ws: gspread.Worksheet | None = None,
    ) -> None:
        """Upsert today's row on this option's dedicated tab.

        Daily rows live on row 6+. If the last non-empty row in column A
        already matches today's date, that row is overwritten (so the
        Apps Script's day-1 row gets healed on the next cron); otherwise
        a new row is appended directly below the last data row.

        Pass `ws` to reuse a worksheet already resolved by the refresh
        loop and skip another ensure round-trip.
        """
        if ws is None:
            ws = self.ensure_dedicated_tab(occ, position)
        row_values = [_serialize(daily.get(h, "")) for h in PER_OPTION_DAILY_HEADERS]
        today_iso = _serialize(daily.get("Date", ""))

        date_col = ws.col_values(_DATE_COL, value_render_option="UNFORMATTED_VALUE")
        # date_col is 1-indexed by row: date_col[0] is row 1's value.
        # Daily data starts at _DATA_START_ROW, so look at date_col[5:] for
        # row 6 onwards.
        data_slice = date_col[_DATA_START_ROW - 1 :] if len(date_col) >= _DATA_START_ROW else []
        last_offset = -1
        for i, v in enumerate(data_slice):
            if v not in ("", None):
                last_offset = i

        if last_offset >= 0:
            last_row = _DATA_START_ROW + last_offset
            if _is_same_iso_date(data_slice[last_offset], today_iso):
                target_row = last_row
            else:
                target_row = last_row + 1
        else:
            target_row = _DATA_START_ROW

        last_col_letter = _col_letter(len(PER_OPTION_DAILY_HEADERS))
        target_a1 = f"A{target_row}:{last_col_letter}{target_row}"
        ws.update(target_a1, [row_values], value_input_option="USER_ENTERED")

    def _write_static_and_headers(self, ws, position: dict[str, Any]) -> None:
        """Write rows 1-3 (static info) + row 5 (daily headers) on a fresh tab.

        Values pulled from the position dict (sourced from the main tracker
        tab). IVx and Range get their value-at-creation snapshots; subsequent
        refreshes do NOT touch this block, matching Stan's manual approach.
        """
        symbol = position.get("Symbol", "")
        company = position.get("Company", "")
        strike = position.get("Strike", "")
        expiry = _serialize(position.get("Expiration", ""))
        purchase = _serialize(position.get("Purchase Date", ""))
        price_paid = position.get("Price Paid", "")
        ivx_raw = position.get("IVx", "")
        vix = position.get("VIX", "")
        range_main = position.get("Range", "")

        title = f"Position: {company} ({symbol}) - {strike} Put"
        row1 = [title]
        row2 = [
            "Symbol:", symbol,
            "Name:", company,
            "Strike:", _serialize(strike),
            "Price Paid:", _serialize(price_paid),
            "IVx:", _format_ivx(ivx_raw),
            "Range:", _format_range_dollars(range_main),
        ]
        row3 = [
            "Expiration:", expiry,
            "Quantity:", 1,
            "Direction:", "Short",
            "Purchase Date:", purchase,
            "VIX:", _serialize(vix),
        ]

        ws.batch_update(
            [
                {"range": "A1", "values": [row1]},
                {"range": "A2", "values": [row2]},
                {"range": "A3", "values": [row3]},
                {"range": f"A{_HEADER_ROW}", "values": [PER_OPTION_DAILY_HEADERS]},
            ],
            value_input_option="USER_ENTERED",
        )
        _apply_static_block_styling(ws, len(PER_OPTION_DAILY_HEADERS))

    def _migrate_flat_layout(self, ws, occ: str, position: dict[str, Any]) -> None:
        """Migrate a tab from the OLD flat-table layout to the new layout.

        Old layout (fe653be / 1b35d64): row 1 = PER_OPTION_HEADERS starting
        with 'OCC'; row 2+ = data rows where the first 8 cols are static
        info repeated each row and the last 11 are daily data.

        We extract the daily portion, reshape it into the new 11-col daily
        schema (computing Difference and Limit on the fly), and rewrite
        the tab with the new static block + headers + salvaged data.
        """
        rows = ws.get_all_values()
        if not rows:
            ws.clear()
            self._write_static_and_headers(ws, position)
            return

        old_headers = rows[0]

        def col_idx(name: str) -> int | None:
            try:
                return old_headers.index(name)
            except ValueError:
                return None

        idx_date = col_idx("Date")
        idx_dte = col_idx("DTE")
        idx_under = col_idx("Underlying")
        idx_strike = col_idx("Strike")
        idx_expiry = col_idx("Expiration")
        idx_occ = col_idx("OCC")
        idx_curprice = col_idx("Current Price")
        idx_pnl = col_idx("P&L")
        idx_range = col_idx("Range")

        migrated: list[list[Any]] = []
        for r in rows[1:]:
            if not any(str(c).strip() for c in r):
                continue

            def take(i: int | None) -> str:
                if i is None or i >= len(r):
                    return ""
                return r[i]

            occ_v = take(idx_occ) or occ
            date_v = take(idx_date)
            expiry_v = take(idx_expiry)
            dte_v = take(idx_dte)
            under_v = take(idx_under)
            strike_v = take(idx_strike)
            cur_v = take(idx_curprice)
            pnl_v = take(idx_pnl)
            range_v = take(idx_range)

            under_f = _to_float(under_v)
            strike_f = _to_float(strike_v)
            range_f = _to_float(range_v)
            diff_v = round(under_f - strike_f, 4) if (under_f is not None and strike_f is not None) else ""
            range_fmt = _format_range_dollars(range_f) if range_f is not None else ""
            limit_v = round(under_f - range_f, 2) if (under_f is not None and range_f is not None) else ""

            migrated.append([
                date_v, occ_v, expiry_v, dte_v,
                _to_float(under_v) if _to_float(under_v) is not None else under_v,
                _to_float(strike_v) if _to_float(strike_v) is not None else strike_v,
                diff_v,
                _to_float(cur_v) if _to_float(cur_v) is not None else cur_v,
                _to_float(pnl_v) if _to_float(pnl_v) is not None else pnl_v,
                range_fmt, limit_v,
            ])

        ws.clear()
        self._write_static_and_headers(ws, position)
        if migrated:
            last_col_letter = _col_letter(len(PER_OPTION_DAILY_HEADERS))
            target_a1 = f"A{_DATA_START_ROW}:{last_col_letter}{_DATA_START_ROW + len(migrated) - 1}"
            ws.update(target_a1, migrated, value_input_option="USER_ENTERED")
        log.info(
            "Migrated %s from flat layout to new layout (%d daily row(s) kept)",
            occ, len(migrated),
        )


def _apply_static_block_styling(ws, daily_col_count: int) -> None:
    """Style rows 1, 2, 3, 5 to match Stan's existing per-option tabs.

    Row 1: title across all columns, merged, dark-blue background.
    Rows 2/3: label cells (odd columns) get medium-blue background;
        value cells (even columns) stay default white.
    Row 5: column headers, dark-blue background.
    Failures here are non-fatal — visual polish should never block a
    daily refresh if the Sheets formatting API hiccups.
    """
    try:
        last_col = _col_letter(daily_col_count)
        # Merge row 1 across the full width so the title spans the tab.
        try:
            ws.merge_cells(f"A1:{last_col}1", merge_type="MERGE_ALL")
        except Exception as e:
            log.debug("merge_cells skipped on %s: %s", ws.title, e)
        ws.batch_format([
            {"range": f"A1:{last_col}1", "format": _TITLE_FMT},
            {"range": "A2", "format": _LABEL_FMT},
            {"range": "C2", "format": _LABEL_FMT},
            {"range": "E2", "format": _LABEL_FMT},
            {"range": "G2", "format": _LABEL_FMT},
            {"range": "I2", "format": _LABEL_FMT},
            {"range": "K2", "format": _LABEL_FMT},
            {"range": "A3", "format": _LABEL_FMT},
            {"range": "C3", "format": _LABEL_FMT},
            {"range": "E3", "format": _LABEL_FMT},
            {"range": "G3", "format": _LABEL_FMT},
            {"range": "I3", "format": _LABEL_FMT},
            {"range": f"A{_HEADER_ROW}:{last_col}{_HEADER_ROW}", "format": _HEADER_FMT},
        ])
    except Exception as e:
        log.warning("Per-option tab %s: styling failed (non-fatal): %s", ws.title, e)


def _occ_hyperlink(occ: str, gid: int) -> str:
    """An in-sheet HYPERLINK formula whose displayed text is the OCC string.

    `=HYPERLINK("#gid=<gid>", "<OCC>")` jumps to the per-option tab when
    clicked but still renders as the plain OCC string, so get_all_records
    (which reads formatted values) keeps matching on it.
    """
    label = str(occ).replace('"', '""')
    return f'=HYPERLINK("#gid={gid}","{label}")'


def _serialize(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float):
        return round(v, 4)
    return v


def _to_float(v: Any) -> float | None:
    """Best-effort float coercion. Handles '±$67.16', '51.0%', and numeric strings."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().lstrip("±").lstrip("$").rstrip("%").replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _format_ivx(v: Any) -> str:
    """Render IVx as '51.0%' (percent scale). Accepts decimal 0-1 or percent 0-100."""
    f = _to_float(v)
    if f is None:
        return ""
    if 0 <= f <= 1.5:
        f = f * 100.0
    return f"{f:.1f}%"


def _format_range_dollars(v: Any) -> str:
    """Render the expected-move dollar value as '±$67.16'. Blank if unusable."""
    f = _to_float(v)
    if f is None:
        return ""
    return f"±${abs(f):.2f}"


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
