/**
 * Track-on-tick handler for the screener Latest tab.
 *
 * When the user ticks a Track checkbox in column S of the Latest tab,
 * copy that row's option into the tracker sheet:
 *   - Append a new row to the main tracker tab.
 *   - Create a dedicated per-option tab (named with the OCC string).
 *   - Write static info at the top + day-1 daily row.
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

const DAILY_HEADERS = [
  'Date', 'DTE', 'Underlying', 'Bid', 'Ask', 'Current Price',
  'P&L', 'IVR', 'VIX', 'IVx', 'Range',
];

const STATIC_HEADERS = [
  'OCC', 'Symbol', 'Company', 'Strike', 'Expiration',
  'Purchase Date', 'Price Paid', 'Limit',
];

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
  const expiry = String(rowData[COL.EXPIRY - 1] || '');
  const bid = Number(rowData[COL.BID - 1]) || 0;
  const ask = Number(rowData[COL.ASK - 1]) || 0;
  const underlying = Number(rowData[COL.UNDERLYING - 1]) || 0;
  const dte = Number(rowData[COL.DTE - 1]) || 0;
  const ivr = Number(rowData[COL.IVR - 1]) || '';
  const expectedMove = Number(rowData[COL.EXPECTED_MOVE - 1]) || '';

  const pricePaid = roundTo(midOf(bid, ask), 4);
  const limit = roundTo(pricePaid * 0.5, 4);
  const purchaseDate = todayIsoEt();

  const tracker = SpreadsheetApp.openById(TRACKER_SHEET_ID);
  const mainTab = findMainTab(tracker);
  if (!mainTab) {
    SpreadsheetApp.getUi().alert(
      'Track failed: could not find the main position tab in the tracker (no sheet has "OCC" in row 1).'
    );
    range.setValue(false);
    return;
  }

  // Don't duplicate — if this OCC is already on the main tab, bail.
  const occColumn = mainTab.getRange(2, 1, mainTab.getLastRow() || 1, 1).getValues().flat();
  if (occColumn.indexOf(occ) >= 0) {
    SpreadsheetApp.getUi().alert(occ + ' is already in the tracker.');
    range.setValue(false);
    return;
  }

  // Main-tab row.
  mainTab.appendRow([
    occ, symbol, company, strike, expiry,
    purchaseDate, pricePaid, pricePaid, 0, 'OPEN',
    ivr, '', '', expectedMove, limit,
  ]);

  // Dedicated tab — create or reuse.
  let dedicatedTab = tracker.getSheetByName(occ);
  if (!dedicatedTab) {
    dedicatedTab = tracker.insertSheet(occ);
  } else {
    dedicatedTab.clear();
  }
  dedicatedTab.getRange(1, 1, 1, STATIC_HEADERS.length).setValues([STATIC_HEADERS]);
  dedicatedTab.getRange(2, 1, 1, STATIC_HEADERS.length).setValues([[
    occ, symbol, company, strike, expiry,
    purchaseDate, pricePaid, limit,
  ]]);
  dedicatedTab.getRange(4, 1, 1, DAILY_HEADERS.length).setValues([DAILY_HEADERS]);
  dedicatedTab.getRange(5, 1, 1, DAILY_HEADERS.length).setValues([[
    purchaseDate, dte, underlying, bid, ask, pricePaid,
    0, ivr, '', '', expectedMove,
  ]]);

  // Reset the checkbox so a second tick on the same row doesn't re-fire.
  range.setValue(false);
}

function findMainTab(tracker) {
  // Stan's workbook has a "Summary" tab at index 0 with merged headers.
  // The position-tracking tab is identified by "OCC" in row 1, not by
  // position — handles any tab order.
  const sheets = tracker.getSheets();
  for (const sh of sheets) {
    const lastCol = sh.getLastColumn();
    if (lastCol < 1) continue;
    const row1 = sh.getRange(1, 1, 1, lastCol).getValues()[0];
    if (row1.some(h => String(h).trim().toUpperCase() === 'OCC')) {
      return sh;
    }
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
