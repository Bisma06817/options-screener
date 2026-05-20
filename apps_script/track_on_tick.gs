/**
 * Track-on-tick handler for the screener Latest tab.
 *
 * When the user ticks a Track checkbox in column S of the Latest tab,
 * copy that row's option into the tracker sheet:
 *   - Append a new row to the main tracker tab.
 *   - Create a dedicated per-option tab (named with the OCC string) in
 *     Stan's legacy layout: rows 1-3 static info, row 4 blank, row 5
 *     daily column headers, row 6+ daily rows.
 *   - Link the main-row OCC cell to that dedicated tab (click to open).
 *   - Reset the checkbox so the same row can't fire twice.
 *
 * Setup (one-time):
 *   1. Open the screener sheet (the one with the Latest tab).
 *   2. Extensions -> Apps Script.
 *   3. Replace Code.gs with this file's contents.
 *   4. Set TRACKER_SHEET_ID below to the ID of Stan's tracker workbook.
 *   5. Save.
 *   6. Triggers (clock icon) -> Add Trigger:
 *        Function: onTickInstalled
 *        Event source: From spreadsheet
 *        Event type: On edit
 *      (Installable trigger — the simple onEdit trigger cannot open
 *       other spreadsheets, only an installable one with the script
 *       owner's auth scope can.)
 *   7. Authorize when prompted.
 */

const TRACKER_SHEET_ID = '1nBrY1Nmwg7u-4-0ygwF9vbddiPaG-ChPblA0dV1jUUs';

// Column indices (1-based) on the Latest tab. Keep in sync with
// OUTPUT_HEADERS in src/screener/sink/sheets.py.
const COL = {
  SCAN_DATE: 1,
  SYMBOL: 2,
  COMPANY: 3,
  OCC: 4,
  STRIKE: 5,
  PUT_PRICE: 6,
  DTE: 7,
  POP: 8,
  IVR: 9,
  DELTA: 10,
  EXPIRY: 11,
  P50: 12,
  BID: 13,
  ASK: 14,
  SPREAD: 15,
  UNDERLYING: 16,
  EARNINGS: 17,
  EXPECTED_MOVE: 18,
  TRACK: 19,
};

const MAIN_HEADERS = [
  'OCC', 'Symbol', 'Company', 'Strike', 'Expiration',
  'Purchase Date', 'Price Paid', 'Current Price', 'P&L', 'Status',
  'IVR', 'VIX', 'IVx', 'Range', 'Limit',
];

// Per-option tab daily-data columns (row 5 headers; row 6+ is one row
// per scan). Must match PER_OPTION_DAILY_HEADERS in sheets_tracker.py.
const PER_OPTION_DAILY_HEADERS = [
  'Date', 'OCC', 'Expiration', 'DTE', 'Share Price', 'Strike',
  'Difference', 'Option Price', 'P&L', 'Range', 'Limit',
];

// Per-option tab layout: rows 1-3 static info, row 4 blank, row 5 header,
// row 6+ daily data.
const PER_OPTION_HEADER_ROW = 5;
const PER_OPTION_DATA_START_ROW = 6;

// Cell styling — matches the colour scheme on Stan's existing per-option
// tabs (dark blue title + header, medium blue label cells, white bold text).
const STYLE_DARK_BLUE = '#1F3864';
const STYLE_MEDIUM_BLUE = '#2E75B6';
const STYLE_WHITE = '#FFFFFF';

function onTickInstalled(e) {
  if (!e || !e.range) return;
  const range = e.range;
  const sheet = range.getSheet();

  if (sheet.getName() !== 'Latest') return;
  if (range.getColumn() !== COL.TRACK) return;
  if (range.getRow() < 2) return;
  if (range.getValue() !== true) return;

  const row = range.getRow();
  const lastCol = Math.max(...Object.values(COL));
  const rowData = sheet.getRange(row, 1, 1, lastCol).getValues()[0];

  const occ = String(rowData[COL.OCC - 1] || '').trim();
  if (!occ) {
    SpreadsheetApp.getUi().alert('Track failed: this row has no OCC Symbol.');
    range.setValue(false);
    return;
  }

  const symbol = String(rowData[COL.SYMBOL - 1] || '');
  const company = String(rowData[COL.COMPANY - 1] || '');
  const strike = Number(rowData[COL.STRIKE - 1]) || 0;
  const expiry = isoDate(rowData[COL.EXPIRY - 1]);
  const bid = Number(rowData[COL.BID - 1]) || 0;
  const ask = Number(rowData[COL.ASK - 1]) || 0;
  const underlying = Number(rowData[COL.UNDERLYING - 1]) || 0;
  const dte = Number(rowData[COL.DTE - 1]) || 0;
  const ivr = Number(rowData[COL.IVR - 1]) || '';
  const expectedMove = Number(rowData[COL.EXPECTED_MOVE - 1]) || '';

  const pricePaid = roundTo(midOf(bid, ask), 4);
  // Limit follows Stan's "maximum price" definition: Share Price minus
  // the dollar expected move (Range value). Used on both the main tab
  // and the per-option daily rows.
  const limit = (isFinite(underlying) && isFinite(expectedMove) && expectedMove !== '')
    ? roundTo(underlying - Number(expectedMove), 2)
    : '';
  const rangeDollarStr = (isFinite(expectedMove) && expectedMove !== '')
    ? '±$' + Math.abs(Number(expectedMove)).toFixed(2)
    : '';
  const difference = (isFinite(underlying) && isFinite(strike))
    ? roundTo(Number(underlying) - Number(strike), 4)
    : '';
  const purchaseDate = todayIsoEt();

  const tracker = SpreadsheetApp.openById(TRACKER_SHEET_ID);
  const found = findMainTab(tracker);
  if (!found) {
    SpreadsheetApp.getUi().alert(
      'Track failed: could not find the main position tab in the tracker ' +
      '(no sheet has an "OCC" header in rows 1-5).'
    );
    range.setValue(false);
    return;
  }
  const mainTab = found.sheet;
  const headerRow = found.headerRow;

  // Don't duplicate — if this OCC is already on the main tab, bail.
  const firstDataRow = headerRow + 1;
  const lastRow = Math.max(mainTab.getLastRow(), firstDataRow);
  const occColumn = mainTab.getRange(firstDataRow, 1, lastRow - firstDataRow + 1, 1).getValues().flat();
  if (occColumn.indexOf(occ) >= 0) {
    SpreadsheetApp.getUi().alert(occ + ' is already in the tracker.');
    range.setValue(false);
    return;
  }

  // Main-tab row. Range column gets the ±$ string; Limit is the raw
  // Share-minus-Range number (numeric so subsequent updates can compute on it).
  mainTab.appendRow([
    occ, symbol, company, strike, expiry,
    purchaseDate, pricePaid, pricePaid, 0, 'OPEN',
    ivr, '', '', rangeDollarStr, limit,
  ]);
  // Force yyyy-mm-dd on the just-written Expiration (col 5) + Purchase Date
  // (col 6) cells so display is locale-independent.
  const mainRowIdx = mainTab.getLastRow();
  mainTab.getRange(mainRowIdx, 5, 1, 2).setNumberFormat('yyyy-mm-dd');

  // Dedicated tab — create or reuse. New layout matches Stan's legacy tabs:
  //   Row 1 : Position title
  //   Row 2 : Symbol/Name/Strike/Price Paid/IVx/Range labels + values
  //   Row 3 : Expiration/Quantity/Direction/Purchase Date/VIX labels + values
  //   Row 4 : blank
  //   Row 5 : daily column headers
  //   Row 6 : day-1 row (this tick)
  let dedicatedTab = tracker.getSheetByName(occ);
  if (!dedicatedTab) {
    dedicatedTab = tracker.insertSheet(occ);
  } else {
    // Existing tab — only re-init if it's NOT already on the new (or
    // Stan's legacy) layout. A leading "Position:" cell means leave it.
    const row1First = String(dedicatedTab.getRange(1, 1).getValue() || '').trim();
    if (!row1First.startsWith('Position:')) {
      dedicatedTab.clear();
    }
  }

  const title = 'Position: ' + company + ' (' + symbol + ') - ' + strike + ' Put';
  const row1 = [[title]];
  const row2 = [[
    'Symbol:', symbol,
    'Name:', company,
    'Strike:', strike,
    'Price Paid:', pricePaid,
    'IVx:', '',          // Apps Script has no IVx; left blank, like Stan's snapshot.
    'Range:', rangeDollarStr,
  ]];
  const row3 = [[
    'Expiration:', expiry,
    'Quantity:', 1,
    'Direction:', 'Short',
    'Purchase Date:', purchaseDate,
    'VIX:', '',          // Apps Script has no live VIX; left blank.
  ]];
  const headerRow = [PER_OPTION_DAILY_HEADERS];
  const dayOne = [[
    purchaseDate, occ, expiry, dte,
    underlying, strike, difference,
    pricePaid, 0, rangeDollarStr, limit,
  ]];

  // Write static block + header only when the tab is fresh or was cleared.
  // If the layout is already correct ("Position:" on row 1), preserve it
  // and just append day-1 below whatever data is already there.
  const row1Now = String(dedicatedTab.getRange(1, 1).getValue() || '').trim();
  if (!row1Now.startsWith('Position:')) {
    dedicatedTab.getRange(1, 1, 1, 1).setValues(row1);
    dedicatedTab.getRange(2, 1, 1, row2[0].length).setValues(row2);
    dedicatedTab.getRange(3, 1, 1, row3[0].length).setValues(row3);
    dedicatedTab.getRange(PER_OPTION_HEADER_ROW, 1, 1, PER_OPTION_DAILY_HEADERS.length)
      .setValues(headerRow);
    styleStaticBlock(dedicatedTab, PER_OPTION_DAILY_HEADERS.length);
  }
  // Append day-1: row 6 if no daily data yet, else the row right below
  // the last non-empty Date cell. Never overwrite existing rows.
  const dateCol = dedicatedTab.getRange('A' + PER_OPTION_DATA_START_ROW + ':A')
    .getValues().map(function (r) { return r[0]; });
  let lastDataIdx = -1;
  for (let i = 0; i < dateCol.length; i++) {
    if (dateCol[i] !== '' && dateCol[i] !== null) lastDataIdx = i;
  }
  const dayOneRow = (lastDataIdx >= 0)
    ? PER_OPTION_DATA_START_ROW + lastDataIdx + 1
    : PER_OPTION_DATA_START_ROW;
  dedicatedTab.getRange(dayOneRow, 1, 1, PER_OPTION_DAILY_HEADERS.length)
    .setValues(dayOne);
  // Format the date cells: Expiration (row 3 col 2), Purchase Date (row 3 col 8),
  // and the daily Date column (col A, row 6 down).
  dedicatedTab.getRange(3, 2).setNumberFormat('yyyy-mm-dd');
  dedicatedTab.getRange(3, 8).setNumberFormat('yyyy-mm-dd');
  dedicatedTab.getRange('A' + PER_OPTION_DATA_START_ROW + ':A').setNumberFormat('yyyy-mm-dd');
  // Expiration column inside the daily table (col C from row 6 down).
  dedicatedTab.getRange('C' + PER_OPTION_DATA_START_ROW + ':C').setNumberFormat('yyyy-mm-dd');

  // Make the main-row OCC cell a clickable link to this option's
  // dedicated tab, so Stan can jump straight to it from the summary.
  // The cell still displays the plain OCC string, so the duplicate
  // check above and the Python refresh both keep matching on it.
  const occCol = MAIN_HEADERS.indexOf('OCC') + 1; // 1-based
  mainTab.getRange(mainRowIdx, occCol).setFormula(
    '=HYPERLINK("#gid=' + dedicatedTab.getSheetId() + '","' +
    occ.replace(/"/g, '""') + '")'
  );

  // Reset the checkbox so a second tick on the same row doesn't re-fire.
  range.setValue(false);
}

function styleStaticBlock(ws, dailyColCount) {
  // Visual styling to match Stan's existing per-option tabs:
  //   Row 1 title           : dark blue background, white bold text, merged
  //   Row 2 / 3 label cells : medium blue background, white bold text
  //   Row 5 column headers  : dark blue background, white bold text
  // Value cells stay default (white background, black text).
  const titleRange = ws.getRange(1, 1, 1, dailyColCount);
  titleRange.merge()
    .setBackground(STYLE_DARK_BLUE)
    .setFontColor(STYLE_WHITE)
    .setFontWeight('bold')
    .setHorizontalAlignment('left');

  // Row 2 has 6 label/value pairs starting at col 1 — labels in odd cols.
  [1, 3, 5, 7, 9, 11].forEach(function (c) {
    ws.getRange(2, c)
      .setBackground(STYLE_MEDIUM_BLUE)
      .setFontColor(STYLE_WHITE)
      .setFontWeight('bold');
  });
  // Row 3 has 5 label/value pairs.
  [1, 3, 5, 7, 9].forEach(function (c) {
    ws.getRange(3, c)
      .setBackground(STYLE_MEDIUM_BLUE)
      .setFontColor(STYLE_WHITE)
      .setFontWeight('bold');
  });

  ws.getRange(PER_OPTION_HEADER_ROW, 1, 1, dailyColCount)
    .setBackground(STYLE_DARK_BLUE)
    .setFontColor(STYLE_WHITE)
    .setFontWeight('bold');
}

function findMainTab(tracker) {
  // Stan's main tab is literally named "Tab" with row 1 as a title row,
  // row 2 blank, and the OCC header on row 3. We try common names first
  // (cheap), then scan the first few sheets for an OCC header in rows
  // 1-5 — permissive 'contains OCC' match so 'OCC Symbol' or 'OCC' both
  // count. Returns { sheet, headerRow } or null.
  const CANDIDATE_NAMES = ['Tab', 'Positions', 'Tracker', 'Open Positions',
                           'Tasty Trade Open Position 2026', 'Summary'];
  const MAX_HEADER_ROW = 5;
  const MAX_SCAN_SHEETS = 10;

  function headerRowOf(sh) {
    const lastCol = sh.getLastColumn();
    if (lastCol < 1) return null;
    const maxRow = Math.min(MAX_HEADER_ROW, sh.getLastRow());
    if (maxRow < 1) return null;
    const rows = sh.getRange(1, 1, maxRow, lastCol).getValues();
    for (let i = 0; i < rows.length; i++) {
      if (rows[i].some(h => String(h).trim().toUpperCase().indexOf('OCC') >= 0)) {
        return i + 1; // 1-based
      }
    }
    return null;
  }

  // Try common tab names first.
  for (const name of CANDIDATE_NAMES) {
    const sh = tracker.getSheetByName(name);
    if (!sh) continue;
    const hr = headerRowOf(sh);
    if (hr !== null) return { sheet: sh, headerRow: hr };
  }

  // Fall back to scanning the first few sheets.
  const sheets = tracker.getSheets().slice(0, MAX_SCAN_SHEETS);
  for (const sh of sheets) {
    const hr = headerRowOf(sh);
    if (hr !== null) return { sheet: sh, headerRow: hr };
  }
  return null;
}

function midOf(bid, ask) {
  if (!isFinite(bid) || !isFinite(ask)) return 0;
  return (Number(bid) + Number(ask)) / 2.0;
}

function roundTo(v, places) {
  const m = Math.pow(10, places);
  return Math.round(Number(v) * m) / m;
}

function todayIsoEt() {
  // Use the spreadsheet's timezone so the date matches what Stan sees.
  const tz = SpreadsheetApp.getActive().getSpreadsheetTimeZone() || 'America/New_York';
  return Utilities.formatDate(new Date(), tz, 'yyyy-MM-dd');
}

function isoDate(v) {
  // Sheets parses our screener's ISO date strings into Date cells, so reading
  // them back yields a JS Date whose default String() is the long timezone
  // form. Re-format anything date-shaped as yyyy-MM-dd; pass strings through.
  if (v instanceof Date && !isNaN(v.getTime())) {
    const tz = SpreadsheetApp.getActive().getSpreadsheetTimeZone() || 'America/New_York';
    return Utilities.formatDate(v, tz, 'yyyy-MM-dd');
  }
  return String(v || '').trim();
}
