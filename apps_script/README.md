# Apps Script — Track-on-tick handler

Lives inside Stan's **screener output sheet** (the one with the Latest tab).
When he ticks a Track checkbox on a Latest row, this script copies that
option into the **tracker sheet** (the separate workbook) and creates a
dedicated per-option tab.

The Python tracker refresh (`src/screener/tracker/refresh.py`, run on the
droplet alongside the daily scan) handles the daily price updates from
then on.

## Install (one-time, by the sheet owner)

1. Open the screener sheet.
2. **Extensions → Apps Script.**
3. Replace `Code.gs` with the contents of `track_on_tick.gs`.
4. Confirm `TRACKER_SHEET_ID` matches the tracker workbook ID.
5. Save (Ctrl+S / Cmd+S).
6. In the left sidebar, click **Triggers** (clock icon) → **Add Trigger**:
   - Function: `onTickInstalled`
   - Event source: **From spreadsheet**
   - Event type: **On edit**
   - Failure notification: Notify me immediately
7. Click Save — Google will prompt for authorization. Accept all scopes
   (script needs access to both the screener and tracker sheets).

A simple `onEdit` trigger is **not** sufficient — simple triggers cannot
open other spreadsheets via `openById`. The installable trigger runs with
the script owner's auth and CAN.

## Verify

1. Open the Latest tab on the screener sheet.
2. Tick a checkbox in column S (Track).
3. The checkbox should reset to unticked within a second.
4. Open the tracker sheet — a new row should appear on the main tab,
   and a new tab should appear named with the OCC string.

If nothing happens, check **Executions** in the Apps Script editor —
errors are logged there.
