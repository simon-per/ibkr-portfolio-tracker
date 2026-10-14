# IBKR Portfolio Analyzer

A full-stack portfolio tracker for an Interactive Brokers account plus a Swiss pillar 3a account. Imports securities, tax lots, trades, corporate actions, dividends and cash from IBKR Flex Queries (and the 3a provider's CSV export), fetches market data, and renders a multi-tab dashboard: total value vs. money in, benchmark comparison, risk metrics, dividends and their forecast, company-level look-through of the ETFs, allocation, a Swiss tax report and more. A separate **Crypto** mode tracks a CoinStats-connected crypto book that shares no figure with the stocks.

Money is stored in EUR and shown in a base currency you switch at will (EUR, CHF or USD); every figure moves with it. The crypto book is kept in USD, as CoinStats reports it.

## Features

### Portfolio
- **Value chart** — Total Value (holdings + cash) against Money In, with a Profit/Loss line and optional benchmark lines (S&P 500 and other indices) anchored to the selected range.
- **Positions** — every holding with gain/loss, weight, yield on cost and a badge for the pillar 3a account. A holding that cannot be valued is shown as unpriced and excluded from totals, never valued at zero.
- **Returns and risk** — XIRR, Modified Dietz, monthly returns heatmap, Sharpe, Sortino, volatility, drawdowns, beta against a selected benchmark.
- **Cash** — IBKR's measured cash balance (Cash Report), so a sale is not drawn as a loss.
- **Multi-currency, multi-exchange** — FX from Frankfurter (ECB) with a bounded fallback; the same stock on two exchanges is two securities keyed on `(ISIN, exchange)`.
- **Two accounts** — the IBKR account and a Swiss pillar 3a account blend into one portfolio everywhere except the tax report and the destructive sync paths.

### Tabs
Performance · Dividends · Look-through · Allocation · Activity · Fundamentals · Watchlist · Tax · Forecast

- **Dividends** — income received by month and on a rolling 12-month basis, a payment calendar, and a forecast built from IBKR's own rates, accruals and measured withholding; its daily history shows what the forecast's growth was made of.
- **Look-through** — company-level exposure with the ETFs decomposed into their published baskets, which refresh themselves.
- **Allocation** — sector, geography and asset class.
- **Activity** — a ledger of trades, dividends and cash movements, exportable as CSV.
- **Fundamentals / Watchlist** — P/E, EPS, PEG, analyst ratings and earnings dates; the watchlist refreshes itself nightly.
- **Tax** — a Swiss tax report: year-end Steuerwert, DA-1 withholding by country, realized gains.
- **Forecast** — projected portfolio trajectory from what has been paid in.

### Automation
- **Scheduled syncs** (APScheduler, Europe/Berlin) — IBKR once a day with a recovery slot, market data several times a day, a nightly watchlist refresh. Both upstreams are rate-limited and the schedule is built around that.
- **Write authentication** — every `POST/PUT/PATCH/DELETE` under `/api/` needs the admin key once `API_ADMIN_TOKEN` is set; reads are open and rate-limited (crypto reads need the key too).
- **Ticker mapping** — IBKR symbols are mapped to Yahoo tickers by exchange suffix and persisted overrides.

## Tech Stack

- **Backend** — FastAPI, SQLAlchemy 2.0 + aiosqlite (SQLite, WAL), Alembic, APScheduler, `ibflex` 0.15, `yfinance` ≥ 1.1.0, `pyxirr`, Frankfurter for FX.
- **Frontend** — React 19 + TypeScript + Vite, TanStack Query, Recharts, Tailwind CSS + shadcn/ui.
- **Deployment** — Docker Compose (a backend container and an nginx container serving the built frontend) behind the host's nginx, which terminates TLS.

## Architecture

```
┌─────────────────┐   ┌──────────────────┐   ┌──────────────────────┐
│ IBKR Flex Query │   │  Yahoo Finance   │   │ CoinStats, CoinGecko │
└────────┬────────┘   │  Frankfurter FX  │   │   (crypto mode)      │
         │            └────────┬─────────┘   └──────────┬───────────┘
         ▼                     ▼                        ▼
┌─────────────────────────────────────────────────────────────────┐
│                       Backend (FastAPI)                          │
│  routers/  → services/ → repositories/ → SQLite (portfolio.db)   │
│  scheduler_service: daily jobs, persistent job store             │
└──────────────────────────────┬──────────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                     Frontend (React + Vite)                      │
│  Stocks: Performance · Dividends · Look-through · Allocation ·   │
│          Activity · Fundamentals · Watchlist · Tax · Forecast    │
│  Crypto: its own view, behind the header's Stocks | Crypto switch│
└─────────────────────────────────────────────────────────────────┘
```

## Prerequisites

- Python 3.11 (what the Docker image and CI run)
- Node.js 20+
- An IBKR Flex Query (see below)

## Installation

### 1. Backend

```bash
cd backend
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
alembic upgrade head
```

### 2. Environment variables

```bash
cp backend/.env.example backend/.env
```

Fill in `IBKR_TOKEN` and `IBKR_QUERY_ID`, and set `SCHEDULER_ENABLED=false` for any local run — otherwise starting the backend arms every scheduled job against the live Flex token and Yahoo. The backend reads `backend/.env` only; a `.env` in the repository root is ignored.

### 3. Frontend

```bash
cd frontend
npm install
```

No frontend env file is needed locally: the API base URL defaults to `http://localhost:8000`.

## IBKR Flex Query Configuration

Format XML. Required sections: **Open Positions** (Lot), **Trades** (Execution), **Cash Transactions** (dividends, payment in lieu, withholding tax, deposits & withdrawals), **Corporate Actions** and **Transfers**. Optional, each read when present: Cash Report (Base Currency Summary), Equity Summary in Base, Open Dividend Accruals, Change in Dividend Accruals, Financial Instrument Information. The fields each section must carry, and why the period matters, are in [docs/flex-and-sync.md](docs/flex-and-sync.md).

## Running Locally

```bash
# Terminal 1 — backend
cd backend
source venv/bin/activate
uvicorn app.main:app --reload --port 8000

# Terminal 2 — frontend
cd frontend
npm run dev
```

- Frontend: http://localhost:5173 (Vite moves to the next free port if it is taken)
- Backend API: http://localhost:8000
- API docs: http://localhost:8000/docs — the authoritative list of every route

## Deployment

A push to `main` deploys automatically once CI is green: a cron job on the server runs `deploy.sh`, health-checks the result and rolls back on failure. By hand, on a configured server:

```bash
cd /root/IBKR_investment_tracker
./deploy.sh
```

Details in [docs/deployment.md](docs/deployment.md) and [DEPLOY.md](DEPLOY.md).

## API Endpoints (main ones)

- **Sync** — `POST /api/sync/ibkr`, `GET /api/sync/status`, `POST /api/market-data/sync`, `GET /api/scheduler/status`, `GET /api/scheduler/history`
- **Portfolio** — `GET /api/portfolio/summary`, `/positions`, `/value-over-time`, `/contributions`, `/annualized-return`, `/attribution`, `/activity`, `/lookthrough`, `/benchmark?benchmark=sp500`
- **Dividends** — `GET /api/dividends/breakdown`, `/summary`, `/forecast-history`
- **Other** — `/api/allocation`, `/api/fundamentals`, `/api/analyst-ratings`, `/api/watchlist`, `/api/tax/report`, `/api/settings`
- **Crypto** (admin key required for every route, reads included) — `GET /api/crypto/portfolio`, `/history`, `/status`, `POST /api/crypto/sync`; see [docs/crypto.md](docs/crypto.md)

## Database Schema (high level)

Core tables: `securities`, `taxlots`, `trades`, `corporate_actions`, `cash_flows`, `cash_balances`, `market_prices`, `exchange_rates`, `ticker_mappings`, `dividend_payments`, `dividend_accruals`, `dividend_forecast_snapshots`, `fundamental_metrics`, `earnings_events`, `analyst_ratings`, `watchlist_items`, `benchmark_prices`, `etf_baskets`, `etf_holdings`, `isin_identities`, `app_settings`, `sync_runs`; the crypto book's own tables are prefixed `crypto_`.

`securities` uses a composite unique key on `(isin, exchange)` so the same ISIN on multiple venues stays distinct.

## Key Design Decisions

### ISIN + exchange as composite key
Same ISIN on different exchanges = separate securities — different prices, currencies and tax lots.

### An unknown is absent, never zero
A holding without a price or FX rate is excluded from both sides of every figure and counted (`unpriced_holdings`), rather than valued at 0.00.

### Yahoo Finance rate limiting
- Random delays per request, Chrome User-Agent, incremental caching (only missing dates are fetched).
- Every loop against Yahoo abandons its pass on a 429.
- Burst limits trigger long cooldowns: stop and wait rather than retrying.

### Modified Dietz + XIRR
Returns are computed both as cash-flow-adjusted Modified Dietz (for monthly figures) and XIRR (money-weighted).

## Troubleshooting

[docs/troubleshooting.md](docs/troubleshooting.md) is indexed by symptom.

**Yahoo Finance 404 / 429 errors** — rate limit hit: stop all market-data requests and wait 30–60 minutes. Confirm `yfinance >= 1.1.0`.

**IBKR `Code=1025`** — a token lockout after repeated failed statement generations. Do nothing; the schedule recovers it, and retrying can extend it.

## Project Structure

```
ibkr-portfolio-tracker/
├── backend/
│   ├── app/
│   │   ├── models/          # SQLAlchemy models
│   │   ├── repositories/    # Data access layer
│   │   ├── services/        # Business logic
│   │   ├── routers/         # API endpoints
│   │   ├── schemas/         # Pydantic models
│   │   ├── cli/             # Write paths run over ssh (imports, mappings, backfills)
│   │   └── main.py
│   ├── alembic/             # Migrations
│   ├── tests/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── components/      # Dashboard, tabs, charts
│   │   └── lib/             # API client and client-side analytics
│   └── package.json
├── docs/                    # One file per subsystem
├── ops/                     # Auto-deploy and backup scripts
├── deploy.sh
└── README.md
```

## License

Private project — not licensed for distribution.
