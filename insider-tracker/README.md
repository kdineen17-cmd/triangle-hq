# Insider Cluster-Buy Tracker → Robinhood Agentic Trading

Watches SEC EDGAR Form 4 filings for **cluster buys**: two or more distinct C-suite officers
or directors of the same small/mid-cap company (< $2B) making **open-market purchases**
(transaction code `P`) within a rolling 30-day window. Each signal can turn into an order
on your Robinhood agentic account, **but only after you approve it at the terminal**.

```
SEC EDGAR ──► tracker.py ──► ClusterSignal ──► main.py guardrail ──► trader.py ──► Robinhood MCP
 Form 4s       parse/filter/      (ticker,       review order, then      OAuth'd MCP     review_equity_order
               cluster/mcap       insiders, $)   "Execute order? (Y/N)"  client          place_equity_order
```

> **This is not investment advice, and the strategy is untested here.** Research suggests
> insider cluster buys have carried a modest edge historically. Form 4s are public within
> two business days, so you are acting on public information, not trading on inside
> information. Small caps have wide spreads, so backtest before you trust it, and keep
> `DRY_RUN=true` until you have.

---

## 1. Setup

```bash
cd insider-tracker
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

Python 3.11+ is required.

## 2. Configure `.env`

| Variable | Required | Meaning |
|---|---|---|
| `SEC_USER_AGENT` | **yes** | `"Your Name you@example.com"`. SEC blocks requests without a contact email. |
| `ROBINHOOD_ACCOUNT_NUMBER` | for trading | The account you enabled for agentic trading in the Robinhood app. Get it from Robinhood's `get_accounts`; it's printed after `--link`. |
| `ROBINHOOD_MCP_URL` | no | Defaults to `https://agent.robinhood.com/mcp/trading`. Must be `https`. |
| `ROBINHOOD_TOKEN_PATH` | no | Where OAuth tokens are stored (0600). Default `~/.config/insider-tracker/robinhood_oauth.json`. |
| `OAUTH_REDIRECT_PORT` | no | Local port for the OAuth redirect (default `8765`). |
| `MIN_INSIDERS` / `CLUSTER_WINDOW_DAYS` | no | Cluster definition (default 2 insiders / 30 days). |
| `MAX_MARKET_CAP_USD` | no | Market-cap ceiling (default `2000000000`). |
| `MIN_CLUSTER_VALUE_USD` | no | Ignore clusters whose total purchases are below this amount (default 0). |
| `DRY_RUN` | no | **Default `true`**: orders are reviewed and logged, never sent. |
| `ORDER_TYPE` | no | `limit` (whole shares, marketable limit at ask + `LIMIT_BUFFER_BPS`) or `market` (dollar-based, fractional, regular hours only). |
| `ORDER_DOLLARS` / `MAX_ORDER_DOLLARS` | no | Target and hard cap per order (default $100 / $250). |
| `MAX_ORDERS_PER_DAY` | no | Hard daily cap on placed orders (default 3). |

`.env` holds only configuration. The app has no Robinhood password or API key setting:
authentication is the OAuth handshake below. Never commit `.env`; it's in `.gitignore`.

## 3. Link Robinhood securely (the OAuth URL handshake)

Robinhood's agentic trading is a remote **MCP server** protected by **OAuth 2.1**. The
app uses the official MCP Python SDK to do the handshake:

1. **Enable agentic trading in the Robinhood app first.** You need a dedicated
   agentic-enabled account; see Robinhood's *Agentic Trading overview* support article.
2. Run:
   ```bash
   python -m src.main --link
   ```
3. The app finds Robinhood's authorization server from the MCP URL, registers itself
   as a public client (no client secret) with a loopback redirect
   `http://127.0.0.1:8765/callback`, and prints an **authorization URL**. It also opens
   the URL in your browser.
4. Sign in **on robinhood.com** and approve access. Check the address bar says
   `robinhood.com` before you enter credentials. The app never sees your password.
5. Robinhood redirects to `127.0.0.1:8765/callback`. A one-shot local listener captures
   the code, which is then exchanged using **PKCE**, so an intercepted code is useless
   on its own. The listener binds only to localhost and shuts down after one request or
   5 minutes.
6. Tokens are saved to `ROBINHOOD_TOKEN_PATH`:
   - The file has `0600` permissions, in a `0700` directory.
   - The app refuses to read the file if its permissions are loosened.
   - Access tokens refresh automatically.
7. The app calls `get_accounts` and checks that `ROBINHOOD_ACCOUNT_NUMBER` exists and is
   enabled for agentic trading. Otherwise it refuses to continue.

To **revoke** access, delete the token file. Also remove the app's access in Robinhood's
settings.

**Remote or headless machines:** the redirect goes to `127.0.0.1` on the machine running
the app. Run `--link` on a machine with a browser, or tunnel the port first:
`ssh -L 8765:127.0.0.1:8765 host`.

## 4. Run

```bash
python -m src.main --backfill --alerts-only -v   # first run: load the last 30 days of Form 4s, print clusters
python -m src.main --once                        # one scan; prompt for each new signal
python -m src.main                               # keep polling (every POLL_SECONDS)
```

The first backfill downloads every Form 4 filed in the window. That's roughly 1–2k
filings per business day, at 8 requests/second, so it can take about an hour. Each
filing is parsed once and cached in `data/insider.db`, so later polls fetch only new
filings.

## 5. The guardrail (what happens on each signal)

1. The cluster is printed: each insider, their role, shares, price and date.
2. `trader.py` builds the order and runs Robinhood's **`review_equity_order`**. This is
   a simulation that places nothing. It returns the quote and pre-trade alerts, which
   are shown to you.
3. The terminal asks:
   ```
   Insiders bought XYZ. Execute order on Robinhood? (Y/N)
   ```
   - Only `Y`/`y` approves. Anything else re-prompts. `N`, Ctrl-C or EOF declines.
   - Keystrokes typed before the prompt appears are flushed.
   - If stdin/stdout isn't a real terminal, the app **refuses to run in trading mode**.
     You can't pipe `yes` into it or run it unattended; use `--alerts-only` for cron or
     servers.
4. Approving creates a `HumanApproval`. It's tied to that reviewed order's `ref_id`
   and a one-time nonce, and is valid for 120 s. `place_equity_order` is called only
   if all of these pass:
   - the approval matches the reviewed order and hasn't expired;
   - the order is under `MAX_ORDER_DOLLARS`;
   - fewer than `MAX_ORDERS_PER_DAY` orders have been placed today;
   - `DRY_RUN=false`.
5. A retry after a network failure reuses the same `ref_id`, so Robinhood de-duplicates
   it and never double-fills.
6. Every signal, decision and order is logged to `data/decisions.jsonl` and
   `data/orders.jsonl`.

## 6. How signals are defined (`src/tracker.py`)

- **Sources:**
  - Backfill uses the EDGAR daily form indexes (`/Archives/edgar/daily-index/`).
  - Live polling uses the EDGAR "latest filings" Atom feed.
  - Each filing's `.txt` submission is fetched once and its `<ownershipDocument>` XML is
    parsed with `defusedxml`.
- **Transactions:** non-derivative only, code `P`, acquired (`A`), with shares and
  price. Form 4/A amendments are skipped.
- **Insiders:**
  - Directors count.
  - Officers count if their title is a C-suite role: any "Chief … Officer" or CxO title
    (CEO, CFO, COO, CTO and so on), even with an EVP or SVP prefix.
  - President and Chair titles count too, unless qualified as Vice, Assistant, Deputy,
    Division or Regional.
  - 10%-only owners are excluded.
- **Cluster:**
  - At least `MIN_INSIDERS` distinct reporting-owner CIKs.
  - Transaction dates within the last `CLUSTER_WINDOW_DAYS` days.
  - Joint filings are de-duplicated.
- **Market cap estimate:** the latest XBRL `dei:EntityCommonStockSharesOutstanding`
  (summed across share classes), multiplied by the latest insider purchase price.
  Issuers with no XBRL data are skipped by default.
- **Re-alerts:** each alert is keyed on issuer + the set of insiders. A new insider
  joining an existing cluster triggers a new alert.

## 7. Backtesting the signal (`backtest/`)

This uses SEC's quarterly Insider Transactions Data Sets (one zip per quarter, the same
filters as the live tracker) plus Robinhood daily prices:

```bash
python backtest/run_backtest.py download --start 2021-01-01   # SEC zips + XBRL share counts (needs sec.gov, data.sec.gov)
python backtest/run_backtest.py events                        # point-in-time cluster events -> data/backtest/events.csv
python backtest/run_backtest.py prices                        # daily bars via Robinhood MCP (uses your --link login)
python backtest/run_backtest.py report                        # event study + $2k portfolio sim
```

- **No lookahead:**
  - An event fires on the filing date when the 2nd qualifying insider's purchase became public.
  - Entry is at the next day's open.
  - A market cap uses the share count from the latest quarter end on or before the filing date.
- **What the report shows:**
  - Returns over 5, 10, 21 and 63 trading days, and excess returns vs IWM, with t-stats and a year-by-year breakdown.
  - Each at 0.15% and 0.40% cost per side.
  - A portfolio simulation with at most 5 positions, each held for 21 days.
- **Bias warning:**
  - Delisted or renamed tickers have no Robinhood price history, so failed companies drop out and the results look better than reality.
  - The report prints the price-data coverage so you can judge how much.

## Layout

```
insider-tracker/
├── .env.example        # copy to .env
├── .gitignore
├── README.md
├── requirements.txt
└── src/
    ├── __init__.py
    ├── tracker.py      # SEC EDGAR Form 4 ingestion + cluster detection
    ├── trader.py       # Robinhood Agentic MCP client, OAuth, risk limits
    └── main.py         # CLI + terminal Y/N guardrail
backtest/
    ├── sec_data.py     # SEC quarterly insider datasets + share counts
    ├── events.py       # point-in-time cluster events
    └── run_backtest.py # download / events / prices / report
```
