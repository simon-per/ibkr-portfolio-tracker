# IBKR Portfolio Analyzer

A full-stack portfolio tracking and analytics application for Interactive Brokers accounts. Imports securities and tax lots from IBKR Flex Queries, fetches market data, and renders a multi-tab dashboard with cost-basis vs. market-value charts, benchmark comparison, fundamentals, dividends, and allocation analytics. All values normalized to EUR.

## Features

### Portfolio Tracking
- **Dual-Line Portfolio Chart** — Cost basis (invested) vs. market value (current worth) over time, with MTD / YTD / 1Y / ALL timeframes.
- **Benchmark Comparison** — Overlay S&P 500 or other benchmarks, cached server-side for fast switching.
- **Summary Cards** — Total value, unrealized gain/loss, realized gain/loss, XIRR (money-weighted return), Modified Dietz returns.
- **Positions List** — All holdings with per-security gain/loss and currency breakdown.
- **Multi-Currency** — Everything converted to EUR via Frankfurter API.
- **Multi-Exchange** — Same stock on different exchanges (e.g., Amazon NASDAQ vs. XETRA) tracked as separate securities keyed on `(ISIN, exchange)`.

### Analytics
- **Allocation Tab** — Treemap visualization by sector, geography, or asset class.
- **Performance Attribution** — Contribution to return by holding.
- **Monthly Returns Heatmap** — Year-over-month performance grid.
- **Fundamentals Tab** — PE, EPS, PEG (derived), forward growth, earnings calendar per security.
- **Watchlist** — Track non-held tickers alongside the portfolio.
- **Dividend Summary** — Historical dividend income by year.
- **Forecast Tab** — Projected portfolio trajectory.

### Automation
- **IBKR Flex Query Sync** — Import securities, tax lots, and reconcile closed positions.
- **Scheduled Jobs** — Daily auto-sync for IBKR, market data, and exchange rates (APScheduler).
- **Ticker Mapping System** — Auto-discovery for international ETFs (German, UK, etc.), with persistent mappings.

## Tech Stack

### Backend
- **FastAPI** — async Python web framework
- **SQLAlchemy 2.0** + **aiosqlite** — async ORM, SQLite storage
- **Alembic** — database migrations
- **ibflex** — IBKR Flex Query XML parsing
- **yfinance** ≥1.1.0 — market data, fundamentals, earnings (requires `lxml` for earnings dates)
- **Frankfurter API** — currency conversion (free, EUR-based)
- **APScheduler** — daily sync jobs
- **pyxirr** — money-weighted return calculation

### Frontend
- **React 18** + **TypeScript** + **Vite**
- **TanStack Query** — data fetching & caching
- **Recharts** — charts, treemaps, heatmaps
- **Tailwind CSS** + **shadcn/ui**

### Deployment
- **Docker Compose** — backend + frontend containers
- **Traefik** — reverse proxy with automatic HTTPS

## Architecture

```
┌─────────────────┐        ┌──────────────────┐
│ IBKR Flex Query │        │  Yahoo Finance   │
└────────┬────────┘        │  Frankfurter API │
         │                 └────────┬─────────┘
         ▼                          ▼
┌─────────────────────────────────────────────────────┐
│                 Backend (FastAPI)                   │
├─────────────────────────────────────────────────────┤
│  Services                                           │
│   • IBKRService        — Flex Query parsing (STK)   │
│   • MarketDataService  — yfinance + ticker mapping  │
│   • CurrencyService    — batch FX with carry-fwd    │
│   • PortfolioService   — cost basis & market value  │
│   • FundamentalsService — PE/EPS + earnings         │
│   • DividendsService   — distribution history       │
│   • BenchmarkService   — S&P 500 / custom indices   │
│   • SchedulerService   — daily APScheduler jobs     │
│                                                     │
│  Repositories                                       │
│   Security · TaxLot · MarketPrice · ExchangeRate    │
│   Fundamental · EarningsEvent · TickerMapping       │
│   Dividend · Watchlist · BenchmarkCache             │
└────────────────────┬────────────────────────────────┘
                     ▼
┌─────────────────────────────────────────────────────┐
│              Frontend (React + Vite)                │
├─────────────────────────────────────────────────────┤
│   Dashboard (tabs): Overview · Allocation ·         │
│   Fundamentals · Watchlist · Forecast               │
│   Charts: PortfolioValueChart · MonthlyReturnsHeat  │
│   Widgets: SummaryCards · PositionsList · Dividends │
└─────────────────────────────────────────────────────┘
```

## Prerequisites

- Python 3.12+
- Node.js 20+
- IBKR Flex Query configured (see below)

## Installation

### 1. Backend

```bash
cd backend

python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

alembic upgrade head
```

### 2. Environment Variables

Create `backend/.env`:

```env
IBKR_TOKEN=your_flex_query_token
IBKR_QUERY_ID=your_flex_query_id
DATABASE_URL=sqlite+aiosqlite:///./portfolio.db
CORS_ORIGINS=http://localhost:5173,http://localhost:5174
```

### 3. Frontend

```bash
cd frontend
npm install
npm run build                     # production
```

Create `frontend/.env.development`:

```env
VITE_API_URL=http://localhost:8000/api
```

## IBKR Flex Query Configuration

**Open Positions** section — required fields:

Currency, Asset Class, Symbol, Quantity, Cost Basis Price, Cost Basis Money, Open Date Time, Description, Listing Exchange, Conid, ISIN, Report Date.

**Also recommended:** Trades (for closed-position reconciliation), CashTransactions (for dividend tracking).

**Settings:**
- Format: XML
- Asset Category: Stocks (STK) only
- Period: Last Business Day (or custom range)
- Date format: `yyyyMMdd`
- Time format: `HHmmss`

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

- Frontend: http://localhost:5173
- Backend API: http://localhost:8000
- API docs: http://localhost:8000/docs

## Deployment

The repo ships a `deploy.sh` script that pulls from GitHub, rebuilds the frontend bundle, and rebuilds the Docker stack. On a configured server:

```bash
cd /root/IBKR_investment_tracker
./deploy.sh
```

Docker Compose manages the backend FastAPI container and an nginx container serving the built frontend. Traefik handles TLS and routing (coexists with other services on the same host).

## Usage

1. **Sync IBKR Data** — click the sync button; securities and tax lots are fetched and reconciled (closed positions marked `is_open = false`).
2. **Sync Market Data** — fetches historical prices from Yahoo Finance into the local cache. ⚠️ Rate-limited; only run when needed.
3. **Explore the dashboard** — switch between Overview, Allocation, Fundamentals, Watchlist, and Forecast tabs.

## API Endpoints

### Sync
- `POST /api/sync/ibkr` — import from IBKR Flex Query
- `GET  /api/sync/status`
- `POST /api/market-data/sync` — fetch historical prices
- `GET  /api/market-data/status`
- `GET  /api/scheduler/status`

### Portfolio
- `GET /api/portfolio/value-over-time`
- `GET /api/portfolio/summary`
- `GET /api/portfolio/positions`
- `GET /api/portfolio/performance-attribution`
- `GET /api/portfolio/monthly-returns`
- `GET /api/portfolio/benchmark` — with `?benchmark=SPY` etc.

### Fundamentals & Analytics
- `GET  /api/fundamentals/{security_id}`
- `POST /api/fundamentals/sync`
- `GET  /api/allocation` — sector / geography / asset class breakdown
- `GET  /api/dividends/summary`
- `GET  /api/analyst-ratings/{security_id}`
- `GET  /api/watchlist` · `POST /api/watchlist` · `DELETE /api/watchlist/{id}`

### Crypto (CoinStats — admin key required for every route, reads included)
- `GET  /api/crypto/portfolio` — totals and holdings from the newest snapshot, in the base currency
- `GET  /api/crypto/history` — daily value and cash-flow-adjusted P&L
- `GET  /api/crypto/status` — last run, next run, CoinStats credits
- `POST /api/crypto/sync` — sync now (at most every 10 minutes); see `docs/crypto.md`

## Database Schema (high level)

Core tables: `securities`, `taxlots`, `market_prices`, `exchange_rates`, `ticker_mappings`, `fundamental_metrics`, `earnings_events`, `dividends`, `watchlist`, `benchmark_cache`.

`securities` uses a composite unique key on `(isin, exchange)` so the same ISIN on multiple venues stays distinct.

## Key Design Decisions

### ISIN + Exchange as composite key
Same ISIN on different exchanges = separate securities — different prices, currencies, and tax lots.

### Batch FX fetching with carry-forward
`CurrencyService` pulls 30-day windows from Frankfurter in a single call, caches everything, and carries the most recent rate forward for weekends/holidays.

### Ticker mapping with auto-discovery
IBKR symbols don't always match Yahoo Finance tickers (e.g., `DBPG@IBIS2` → `DBPG.DE`, `SMH@LSEETF` → `SMH.L`). The service checks custom mappings first, then applies exchange suffixes (`XETRA/IBIS2 → .DE`, `LSEETF → .L`, `AEB → .AS`), then tries variations. Successful discoveries are persisted.

### Yahoo Finance rate limiting
- yfinance ≥ 1.1.0 with Chrome User-Agent headers
- 1–3 s random delay per request, 2–4 s between securities
- Incremental caching — only missing dates are fetched
- ⚠️ Always get user permission before running `/api/market-data/sync`. Burst limits trigger ~1 hour cooldowns.

### Modified Dietz + XIRR
Returns are computed both as cash-flow-adjusted Modified Dietz (for monthly bars) and XIRR (for the headline annualized figure).

## Troubleshooting

**IBKR sync returns 0 securities**
Check backend logs for the `AssetClass` enum debug; verify the XML has `assetCategory="STK"`.

**Currency conversion failing**
Confirm the base URL is `https://api.frankfurter.app` (not `.dev`). Recent same-day rates may not be published yet — carry-forward should handle it.

**Market prices missing for a security**
Inspect ticker mapping debug output; add a manual mapping via `TickerMappingRepository`; ensure the exchange suffix is registered in `EXCHANGE_SUFFIXES`.

**Yahoo Finance 404 / 429 errors**
Rate limit hit — stop all market-data requests and wait 30–60 minutes. Confirm `yfinance >= 1.1.0` and that delays are active.

**Reset database**
```bash
rm backend/portfolio.db
cd backend && alembic upgrade head
```

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
│   │   └── main.py
│   ├── alembic/             # Migrations
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── frontend-nginx.conf
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── components/      # Dashboard, charts, tabs
│   │   └── lib/             # API client
│   └── package.json
├── deploy.sh                # Server deployment script
└── README.md
```

## Roadmap

- [ ] Tax-lot-level sale tracking with cost-basis methods (FIFO, LIFO, specific ID)
- [ ] Corporate actions (splits, spin-offs)
- [ ] Multi-portfolio support
- [ ] Dark mode polish across all tabs
- [ ] CSV / Excel export

## License

Private project — not licensed for distribution.
