# Options Screener (Phase 1)

Cloud-hosted daily scan that screens a watchlist for short-put candidates and
writes ranked results to a Google Sheet.

**Architecture (per the brief):**

```
Cron on a DO droplet
   │
   ▼
Docker container
   │
   ├─ Claude (Opus 4.7, Anthropic API) ── tasty-agent (MCP, OAuth2) ── Tastytrade
   │                                  └── Yahoo Finance (yfinance, fallback)
   │
   └─ Writes results → Google Sheet
```

- **Claude is the agent.** Adaptive thinking + `effort: high`. The system
  prompt is prefix-cached so repeat scans within a 5-minute window pay
  ~0.1× input price on the cached portion.
- **tasty-agent** ([repo](https://github.com/ferdousbhai/tasty-agent)) is the
  MCP server that wraps the tastytrade API. OAuth2 only. Primary source for
  prices, greeks, IVR, IVx, and `expected_report_date` (earnings).
- **Yahoo Finance** (`yfinance`, no API key) is the fallback for earnings
  dates when tastytrade does not provide one, and the source for company
  names. All Yahoo calls are retried with exponential backoff; failures are
  logged and the scan continues.
- **DigitalOcean** hosts the droplet ($6/mo).
- **Google Sheets** is the watchlist + config + output layer. All Sheets
  reads/writes use exponential backoff (`tenacity`).

Phase 2 will add paper-trade execution; Phase 3 adds live trading. The
`execute/` module is the seam where those plug in.

## What you do, once

The setup is one-time. Plan ~45 minutes the first time through.

### 1. Spreadsheet (1 min)

Already done — sheet ID `12MFqvhDO9uTCW-IhESyZ_HBqmYtPdUOJDBtxRmwa6HA`.

### 2. Google service account (10 min)

Console → new project → enable **Google Sheets API** → IAM →
**Service Accounts** → create → **Keys → Add → JSON** (downloads file).
Open the sheet → **Share** → paste the service account's email → Editor.
Keep the JSON file — you'll paste its contents into `.env` later.

### 3. Anthropic API key (2 min)

Sign up at https://console.anthropic.com/, create a key. Cost: ~$0.10–0.50 per
scan (Opus 4.7 with caching). Budget ~$5–15/month.

### 4. Tastytrade OAuth2 app (10 min)

Tasty-agent only supports OAuth2 — username/password is not an option. So:

1. Sign in to https://developer.tastytrade.com/ (use your normal tastytrade login).
2. Register an app. **Set the redirect URI to** `http://localhost:8765/callback`.
3. Copy the **Client ID** and **Client Secret** that the page shows.
4. On your laptop:
   ```
   pip install requests
   python scripts/oauth_setup.py
   ```
   The script asks for the Client ID and Secret, opens your browser to log
   into tastytrade, captures the authorization code, and prints three values
   you'll need: `TASTYTRADE_CLIENT_SECRET`, `TASTYTRADE_REFRESH_TOKEN`,
   `TASTYTRADE_ACCOUNT_ID`. Save these — they're long-lived.

### 5. DigitalOcean droplet (15 min)

1. Sign up / log in at https://cloud.digitalocean.com/.
2. Create a droplet:
   - Region: closest to you (latency doesn't matter much for a daily scan)
   - Image: **Ubuntu 24.04 LTS x64**
   - Size: **Basic → Regular → $6/mo** (1 GB RAM is enough)
   - Auth: SSH key (paste your laptop's public key)
3. Once it boots, SSH in: `ssh root@<droplet-ip>`
4. Run the setup script:
   ```
   curl -fsSL https://raw.githubusercontent.com/Bisma06817/options-screener/main/deploy/setup_droplet.sh \
     | bash
   ```
   (Replace the URL if your repo lives elsewhere.) This installs Docker,
   clones the repo, builds the image, and installs the hourly cron job.
5. Edit `/opt/options-screener/.env` and fill in all six values from
   steps 2–4 above. The `GOOGLE_SERVICE_ACCOUNT_JSON` value is the **entire
   JSON file**, on one line — `cat downloaded.json | tr -d '\n'` is your
   friend.
6. Force-run once to confirm everything is wired correctly:
   ```
   cd /opt/options-screener
   docker run --rm --env FORCE_RUN=1 --env-file .env options-screener:latest
   ```
   Watch the output. The sheet's `Logs` tab also gets a row.

### 6. GitHub repo (3 min, optional but recommended)

If you want the code in your own repo:

```
cd C:\Users\Admin\options-screener
git init
git add .
git commit -m "Initial scaffold"
gh repo create options-screener --private --source=. --remote=origin --push
```

Then update `REPO_URL` at the top of `deploy/setup_droplet.sh` to point at
your repo before running it on the droplet.

## How you use it day-to-day

### Edit the watchlist

`Watchlist` tab, column A. Add or remove tickers freely — no code changes.

### Tune the scan time

`Config` tab → row `scan_time_et`. Use 24-hour ET like `15:30`. The cron
fires hourly during US market hours; the script runs only if current ET
time is within `scan_window_minutes` of your target. So you can experiment
with `09:30`, `12:00`, `15:30`, `15:55` etc. without redeploying.

### Tune the filters

`Config` tab → `ivr_min`, `dte_min`, `dte_max`, `delta_min`, `delta_max`.

### Read results

- `Latest` tab — today's results, overwritten each scan.
- `History` tab — every scan's results appended.
- `Logs` tab — one row per run with status (`ok` / `error` / `skipped`)
  and any error message.

### Force a run

```
ssh root@<droplet-ip>
cd /opt/options-screener
docker run --rm --env FORCE_RUN=1 --env-file .env options-screener:latest
```

## Output columns

`Scan Date, Symbol, Company, Strike, Put Price, DTE, POP%, IVR%, Delta,
Expiry Date, P50%, Bid, Ask, Spread, Underlying Price, Earnings Date,
Expected Move`

Notes:
- **Company** is the long name from Yahoo Finance (`yfinance`), cached
  per-process. Falls back to the ticker symbol if Yahoo is unreachable.
- **Put Price** = bid-ask mid.
- **POP%** = `(1 − |delta|) × 100`. Standard short-put proxy.
- **P50%** is left blank in Phase 1. Tastytrade's P50 is a Monte Carlo from
  the desktop UI, not in the API. Phase 2 will add a Monte Carlo step.
- **Expected Move** = `Spot × IVx × √(DTE/365)` using IVx from
  `get_market_metrics`.
- **Earnings Date** comes from tastytrade's `get_market_metrics`
  (`expected_report_date`) first; if that's missing for a symbol, Claude falls
  back to Yahoo Finance. If neither source has a date, the cell is blank.

## Project structure

```
src/screener/
  config.py                 env loading + filter defaults
  main.py                   entrypoint, time-window gate
  agent/
    screener_agent.py       Claude + tasty-agent MCP loop
  data/
    yahoo.py                Yahoo Finance client (earnings fallback + company name)
  screen/
    filters.py              pure filter / rank functions (also unit-tested)
  sink/
    sheets.py               Sheets read/write (with tenacity retries)
  execute/
    stub.py                 Phase 2/3 placeholder
scripts/
  oauth_setup.py            one-time tastytrade OAuth helper
deploy/
  setup_droplet.sh          run on a fresh DO droplet to bootstrap everything
Dockerfile                  Python 3.11 + tasty-agent + this app
.env.example                template for the six required env vars
```

## Local development (optional)

```
pip install -r requirements.txt
PYTHONPATH=src pytest                                    # unit tests, no network
cp .env.example .env  &&  edit it
PYTHONPATH=src FORCE_RUN=1 python -m screener.main       # full live run
```

## Rate limiting, retries, and logging

Per the brief, the screener handles transient failures and surfaces them
loudly rather than silently:

- **Exponential backoff (tenacity).** All Google Sheets reads/writes and all
  Yahoo Finance calls retry up to 3 times with 1–8 s exponential backoff. A
  retry attempt is logged at WARNING with the exception, so a flaky network
  shows up in the Logs tab without aborting the scan.
- **Anthropic SDK** has its own internal retry on 429 / 5xx, so Claude calls
  inherit that for free.
- **tasty-agent** runs as a child stdio process — its own client handles
  tastytrade API retries internally.
- **Errors are visible, not silent.** Any unhandled exception in a scan is
  caught in `main.run()`, written to the `Logs` tab with status `error` and
  the exception class + message, and the run exits 1 so cron stderr captures
  it too.
- **Request queuing** is **not** implemented. With a 15-symbol watchlist
  scanned once per market day, throughput is ~200–300 tool calls per scan,
  which is well under tastytrade's published limits and Anthropic's per-key
  rate limits. Adding an explicit semaphore would add complexity without
  measurable benefit at this scale. Phase 2 (paper trading) is the natural
  point to add a queue if the workload grows.

### Full API response logging during testing

The brief asks for "All API responses logged during the initial testing
phase." Set `LOG_LEVEL=DEBUG` in the droplet's environment to enable this:

```
docker run --rm --env LOG_LEVEL=DEBUG --env FORCE_RUN=1 --env-file .env options-screener:latest
```

At DEBUG level the screener logs:

- The full agent **user prompt** (watchlist, filter thresholds, today's date)
- The full **MCP tool schemas** advertised by tasty-agent
- Every Anthropic **message body** the runner produces (every content block,
  including tool calls and tool results, with token usage)
- The raw **Yahoo Finance** calendar response and resolved company name per
  symbol

Switch back to `LOG_LEVEL=INFO` once initial testing is done — DEBUG output
is verbose and contains symbol-level data you don't want in the production
log file long-term.

## Cost

| Item                          | Cost                  |
|-------------------------------|-----------------------|
| DigitalOcean droplet          | $6 / month            |
| Anthropic API (Opus 4.7)      | ~$5–15 / month at 1 scan/day with caching |
| Google Cloud (Sheets API only)| Free                  |
| Tastytrade API                | Free with brokerage account |
| **Total**                     | **~$11–21 / month**   |

## Phase 2 / 3

The architecture is modular: `execute/stub.py` is the only file that needs
swapping to add paper trading (Phase 2) or live execution (Phase 3). The
agent already has access to the tasty-agent `place_order` tool, but it is
explicitly blocked in `_BLOCKED_TOOL_NAMES` (in `screener_agent.py`) for
Phase 1. Lifting that block + a permission/confirmation step is the Phase 3
delta.
