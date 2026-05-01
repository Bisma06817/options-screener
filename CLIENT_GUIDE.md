# Options Screener — Client Guide

This guide is for the **end user** of the screener. Everything in here is done in the Google Sheet — no terminal, no code, no developer.

---

## What the system does

Once a day, at the time you set, the system:

1. Reads your watchlist of tickers from the Sheet
2. Pulls live options data from your tastytrade account
3. Filters for trades that match your criteria (IVR, DTE, delta, no earnings in window)
4. Writes the qualifying candidates to the Sheet

Everything runs in the cloud, 24/7. Your computer does not need to be on.

---

## The Sheet — what each tab is for

| Tab | What it's for | Edit? |
|---|---|---|
| **Watchlist** | The tickers to scan | ✅ You edit |
| **Config** | Filter thresholds and scan time | ✅ You edit |
| **Latest** | Today's qualifying candidates (overwritten each scan) | ❌ Read-only |
| **History** | Every candidate from every scan, appended forever | ❌ Read-only |
| **Logs** | One row per scan run, with status | ❌ Read-only |

---

## How to manage the watchlist

Open the **Watchlist** tab.

- **Add a ticker:** type the symbol (e.g. `AAPL`) in a new row under the `Symbol` column
- **Remove a ticker:** delete that row entirely
- **No restart needed.** The next scan will pick up the change automatically

Tip: keep the list under ~30 tickers. Each ticker adds ~10–20 seconds to the scan.

---

## How to change the scan time

Open the **Config** tab. Find these two rows:

| key | value | meaning |
|---|---|---|
| `scan_time_et` | `15:30` | Target scan time in New York time (24-hour) |
| `scan_window_minutes` | `30` | How close to that time the scan should fire |

**To change the scan time:** edit the `scan_time_et` value. Examples:

| Value | Runs at |
|---|---|
| `09:30` | Market open |
| `12:00` | Noon |
| `15:30` | 3:30 PM (default — late afternoon) |
| `15:45` | 15 min before close |

**Time zone is automatic.** Always enter New York wall-clock time. The system handles daylight saving on its own.

**Once-daily guard:** the system runs at most one successful scan per New York calendar day. Even if the scan window is wide and multiple cron fires fall inside it, only the first will scan; the rest will exit quietly. So `scan_window_minutes` is safe at any reasonable value.

---

## How to change filter thresholds

Open the **Config** tab. Edit these values:

| key | default | meaning |
|---|---|---|
| `ivr_min` | `50` | Minimum IVR % required |
| `dte_min` | `30` | Minimum days to expiration |
| `dte_max` | `60` | Maximum days to expiration |
| `delta_min` | `0.15` | Minimum absolute delta |
| `delta_max` | `0.25` | Maximum absolute delta |

Save (Sheets auto-saves). The next scan uses the new values.

To experiment, lower `ivr_min` to `0` temporarily — you'll see many more candidates appear. Reset to `50` afterward.

---

## How to verify the system is working

### Daily check (10 seconds)

1. Open the **Logs** tab
2. Confirm the most recent row:
   - Has today's date in `Timestamp UTC`
   - `Status` column = `ok`
   - `Symbols Scanned` matches the number of tickers in your Watchlist

If `Status` = `ok` → the scan ran successfully. Done.

### Reading the Latest tab

The **Latest** tab shows today's qualifying candidates.

- **Has rows** → those are today's trades, ranked by IVR % descending
- **Empty (just headers)** → no stocks passed all filters today

If `Latest` is empty, **cross-check the Logs tab**:
- If `Status: ok` and `Rows: 0` → the scan ran fine, just no qualifying trades (normal during earnings season)
- If `Status: error` → something broke, see the `Error` column for details

### Reading scan times in the Logs

Logs use **UTC** time. Convert to NY time:

- Summer (EDT, March–November): `UTC time − 4 hours = NY time`
- Winter (EST, November–March): `UTC time − 5 hours = NY time`

So if you set `scan_time_et = 15:30`, expect log timestamps around:
- Summer: `19:30 UTC`
- Winter: `20:30 UTC`

### Output column reference

The **Latest** and **History** tabs have these columns:

| Column | Meaning |
|---|---|
| Scan Date | When this candidate was found |
| Symbol | Ticker |
| Company | Company name |
| Strike | Put strike price |
| Put Price | Mid price (bid + ask) / 2 |
| DTE | Days to expiration |
| POP% | Probability of profit (≈ 1 − \|delta\|) |
| IVR% | Implied volatility rank |
| Delta | Option delta (negative for puts) |
| Expiry Date | Expiration date |
| P50% | Probability of 50% profit (placeholder for Phase 2) |
| Bid | Current bid |
| Ask | Current ask |
| Spread | Ask − Bid |
| Underlying Price | Current stock price |
| Earnings Date | Next earnings date (if any) |
| Expected Move | Underlying × IVx × √(DTE/365) |

---

## Quick "is it alive?" test

Anytime you want to confirm the system is healthy:

1. **Config** tab → change `scan_time_et` to a time about 5 minutes from now (NY time)
2. Wait for the next top-of-hour
3. Refresh the **Logs** tab — a new row should appear within 10 minutes
4. Change `scan_time_et` back to your preferred time

If a new log row appears → system is working.

---

## When to contact the developer

Open a support ticket / send a message if you see any of these:

| Symptom | Likely cause |
|---|---|
| No new row in **Logs** for 2+ days | Cloud server down, or cron stopped |
| `Status: error` two days in a row | API token expired, or upstream change |
| `Symbols Scanned: 0` with a non-empty Watchlist | Watchlist read failure |
| `Latest` has rows with blank prices | Live data feed issue |
| Tastytrade refresh token expired (errors mentioning auth/oauth) | Need to regenerate Personal Grant token |

---

## Phase 2 / Phase 3 — what's next

This guide covers **Phase 1** only — read-only screening, no order placement.

- **Phase 2** will add paper-trading simulation (system places orders in tastytrade's paper environment)
- **Phase 3** will add live trade execution

Both phases will continue to use the same Sheet for input and output. The columns and tabs may grow, but watchlist/config editing will stay the same.
