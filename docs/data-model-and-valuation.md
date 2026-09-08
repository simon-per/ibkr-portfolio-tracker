# Schema, reconciliation, realized P&L, the timeline walk

> Moved verbatim from `CLAUDE.md` on 2026-09-08, when that file became a short index so a
> session no longer loads ~92k tokens of documentation to start. **This file is
> authoritative for its subsystem**: every invariant here was a bug first. Keep it accurate
> the way CLAUDE.md was kept — update it in the same change that moves the code it describes.

## Database schema

- **securities** — composite identity `isin + exchange` (same stock on two exchanges = two rows, e.g.
  ASML on NASDAQ *and* AEB). Also `symbol`, `description`, `currency`, `conid` (**nullable** —
  IBKR-only, and `SecurityRepository.upsert` refuses a NULL because `conid == None` renders
  `IS NULL` and would match an arbitrary conid-less row), **`account`** and **`price_source`**
  ∈ `yahoo` | `manual` — see *A second account*.
- **taxlots** — one row per purchase: `open_date`, `quantity`, `cost_basis`, `cost_basis_eur`
  (pre-converted), `is_open`, `close_date`, **`close_source`** ∈ `trade` | `corporate_action` |
  `heuristic`.
- **trades** — authoritative executions, idempotent on `ib_key`: `buy_sell`, `quantity`, `price`,
  `proceeds`, `commission`, **`realized_pnl`** (IBKR's own FIFO). `security_id` is nullable — a fully
  sold security is no longer in OpenPositions.
- **corporate_actions** — `action_type` (Reorg name), `quantity`, `value`, `proceeds`, `description`.
- **cash_flows** — external cash, idempotent on `ib_key`: `flow_date`, **`flow_type`** ∈
  `DEPOSITWITHDRAW` | `TRANSFER_IN` | `TRANSFER_OUT` | `TRANSFER`, `amount` (signed as IBKR reports:
  deposit +, withdrawal −), `amount_eur` (pre-converted at `flow_date`). Only `DEPOSITWITHDRAW` counts as
  money added — see the contributions section.
- **dividend_payments** — `gross_amount_eur`, **`withholding_tax_eur`**, **`net_amount_eur`**,
  `pay_date`, **`source`** ∈ `ibkr` | `yfinance_estimate`.
- **exchange_rates** / **market_prices** — caches. **ticker_mappings** — IBKR→Yahoo symbols.
- **isin_identities** — ISIN → company identity, keyed by ISIN: `lei` + `issuer_name` +
  `lei_source` + `lei_checked_at` (GLEIF), `share_class_figi` + `composite_figi` + `figi_name` +
  `figi_source` + `figi_checked_at` (OpenFIGI). Per-provider stamps because they fail
  independently in both directions; a NULL identifier beside a non-NULL stamp means *asked, no
  record*. See *Look-through*.
- **etf_baskets** / **etf_holdings** — one fund's constituent basket, keyed by **fund ISIN** (a
  basket belongs to the share class, not the venue, so one fund held on two exchanges shares it).
  Metadata is split from the rows on purpose; `weight_pct` is a **percent**;
  `UNIQUE(fund_isin, line_no)` because `constituent_isin` is nullable and SQLite treats NULLs as
  distinct. Replaced wholesale, never merged row-by-row.
- **cash_balances** — IBKR's own end-of-day `cash` / `stock` / `total` per `report_date`, from
  `<EquitySummaryInBase>`. **Empty until that section is enabled in the Flex portal, and that is a
  supported state** — `CashService` derives a balance instead and every surface badges which. In
  IBKR's *account* base currency, which `currency` records rather than assumes.
- **app_settings** — `base_currency`, `last_sync_to_date`. Plus fundamentals + earnings tables.
- **sync_runs** — one row per sync attempt (`sync_type` ∈ `ibkr` | `ibkr_sync` | `full_sync` |
  `market_data_only` | `ibkr_manual_xml` | `manual_prices` | `manual_mapping` | `manual_cash_flow` |
  `manual_dividend_prune` | `manual_dividend_purge` | `manual_identity_resolve` |
  `manual_etf_basket` | `pillar3a_csv`,
  `status`, `message`,
  `details`, `warnings`). Timestamps are
  serialized UTC-aware via `utc_iso()` — a bare naive `isoformat()` is parsed as *local* by the browser,
  which once made an 08:00 sync display as 06:02.
  `SchedulerService.last_sync_result` is in-memory only and auto-deploy restarts on every push, so
  without this the daily validator can't tell "no sync ran" from "the container restarted".
  `/api/scheduler/status` falls back to it; `/api/scheduler/history?limit=N` lists recent runs.
  Recording is best-effort — it must never fail a sync or mask the real error.

**Every stored timestamp is a naive UTC datetime, and `app/clock.py` is the only way to make one.**
The columns default to `func.now()`, which SQLite answers in UTC, and `utc_iso()` serializes by
stamping UTC onto a value it *assumes* is already UTC. The stdlib's argument-less `now()` returns naive
**local** time, so it was correct at ~49 call sites only because `python:3.11-slim` sets no `TZ` — an
undeclared dependency on the base image that nothing would have noticed breaking. Off the container it
was already wrong three ways at once: cache cutoffs (`now - timedelta(days=7)` against a naive-UTC
column) expired an offset early, ages (`now - row.last_synced`) read an offset too old, and
`utc_iso(datetime.now())` labelled local time as UTC so the browser converted it a second time — the
"two clocks on one line" failure `utc_iso` exists to prevent, reintroduced through its own argument.
`utcnow()` stays **naive** on purpose: these values are compared against naive columns, where an aware
value raises `TypeError` rather than degrading. `tests/test_clock_convention.py` walks the source tree
so the old call cannot return, and asserts against real UTC rather than local — the assertion that
passes vacuously on the container and catches the bug everywhere else.

Migrations: `cd backend && alembic upgrade head` (the container CMD runs this on every start).

---

## Reconciliation & realized P&L

`reconcile_taxlots()` (`sync_helper.py`) explains quantity changes in priority order:
1. **SELL trades** → close lots FIFO with the real date/proceeds/`fifoPnlRealized`; `close_source='trade'`
2. **Corporate actions** → deterministic reclassification (split/spinoff/merger), not a sale
3. **Fallback heuristic** (quantity drop + `COST_CONSERVED_RATIO`) → `close_source='heuristic'`

**That order is the code's order, and it wasn't until 2026-07-30.** The trade lookup used to run
*third*, supplying only a date and a provenance tag after 2 and 3 had already decided — so a trim of
**≤1% of cost basis** took the cost-conserved path and recorded **no closure at all** while IBKR's own
SELL sat in `trades` for that window (200 shares at 20,000 sold down by 2 leaves 19,800, and
`19800 >= 20000 * 0.99`). The disposal then never reached `calculate_xirr()`'s `+proceeds` inflow, the
attribution's disposal term, that day's `external_flow_eur`, or the tax report's `closed_lot_estimate`.
The two inferences now run **only when no SELL is on record**, so the reverse-split and cash-in-lieu
protections are untouched — pinned from both sides in `tests/test_sync_helper.py`.

**`open_date` comes off `position.openDateTime`, parsed by ibflex.** It used to be scraped from the raw
XML into a separate list and matched back by position index, but that list kept only STK rows carrying
the attribute while the loop enumerated *every* `OpenPosition` — so one bond/option row, or one lot
without the attribute, shifted every index after it, and the `conid` check fell back to `reportDate`
rather than realigning. One stray row silently stamped **every later lot** with the statement date.
ibflex 0.15 does parse the field (`parse_element_attr` returns a `datetime`); the comment claiming
otherwise was wrong. Don't reintroduce index matching. Tests: `tests/test_ibkr_parsers.py`.

`restamp_unsourced_closed_lots()` then fixes lots closed *before* `<Trades>` existed: those carry the
date the sync **noticed** the drop, not the sale date (CRM and NFLX read 2026-04-17 for a 2026-03-13
sale). A lot is only re-stamped when exactly one SELL for that security matches its quantity and isn't
newer than the recorded close date — ambiguity is left alone rather than guessed. Idempotent (a stamped
lot has a `close_source`), so it self-disables.

Note `conid_to_security_id` is built from the statement's **OpenPositions**, so a security sold out
entirely isn't in it; `persist_transactions` falls back to a DB lookup by conid, otherwise every SELL
trade lands with `security_id = NULL`.

**Empty-statement wipe guard:** if incoming tax lots are empty but the DB holds open lots, the sync
**aborts** instead of marking everything sold. A successful-but-empty statement is treated as a failure.
This guard has already saved the data through several failed syncs.

`get_realized_totals()` prefers `trades` (exact) and falls back to a market-price approximation over
closed lots. `realized_rows_from_closed_lots()` is **shared** by the portfolio totals and the tax report
so the two can never disagree — they did once, and that was a bug. The picker keys on **SELL trades
specifically**, not on the table being non-empty: a BUY-only statement returned hard zeros and
permanently suppressed the fallback, while the tax report (which decides per year) still showed real
gains. Pinned by `tests/test_realized_totals.py`.

**A lot sold on D is not held at D's close.** One convention, everywhere: `_calculate_daily_value`,
`holdings_snapshot_as_of()` and the attribution gates all exclude **on** the close date, matching what
the benchmark always did. Under the old include-on-close rule a same-day rotation double-counted that
day (the sold lot still "active" beside its replacement) and a position sold on 31 December landed in
both the year's Steuerwert *and* its realized gains. Consequence to expect: a sale's value leaves the
chart on the sale date itself, one day earlier than before. The disposal windows in `calculate_xirr()`
and the attribution endpoint are therefore `(start, end]` — a lot sold *on* the window end yields
proceeds precisely because the end valuation no longer carries it. Change these together.
Tests: `tests/test_close_date_boundary.py`.

**A spun-off line is not held before the action that created it, even though its lots say it
was.** IBKR reports received spinoff shares against the *parent's* tax lots — the child
inherits the parent's `openDateTime`, because the holding period carries over, plus the slice
of cost basis IBKR reallocates to it. So the lot asserts ownership from a date when the
instrument had no listing and no price, and every valuation in that gap counted a held
security it could not value. Read off production on 2026-08-07: **MBGL, spun out of SPGI
1-for-1 on 2026-06-30 against lots dated 2025-11-06 and 2025-12-29**, made
`unpriced_holdings = 1` on **166 consecutive days**, and the client correctly refused every
one of them.

The damage was entirely in what the flag *implies*, which is why it survived: the position is
0.2% of the book, so no total looked wrong. What broke was the monthly-returns table —
December 2025 through May 2026 blank, November 2025 measured over three days, and a **"YTD"
of +3.1% that covered 26 June to 7 August**. The full-year figure is +23.5%.

`PortfolioService._load_position_start_dates()` reads
`sync_helper.POSITION_CREATING_ACTIONS` and floors the date each security counts as *valuable*
at the action that created it. Four things about it are load-bearing:

- **Excluding the child is the arithmetic, not a workaround for the missing prices.** We fetch
  `auto_adjust=False` and Yahoo does not rebase raw `Close` for a spinoff (the same reason
  `PRICE_RESTATING_ACTIONS` excludes one), so the parent's own close still carried the
  spun-off business on those days. Valuing the child beside it double-counts — which it did,
  for the four when-issued days between the first MBGL bar and the distribution.
- **Its cost stays where the lot puts it.** IBKR reduced the parent's basis by exactly what
  the child received (verified: 4.84% of each SPGI lot), so the pair sums to the original
  outlay on the original date, and the day's `external_flow_eur` is the real purchase.
  Deferring the cost too would understate the cost line before the spinoff and book a phantom
  purchase on the day of it.
- **The action set is far narrower than `SPLIT_LIKE_ACTIONS`, in both directions.** A forward
  split also adds shares, but to a security already held, so flooring on one would drop a
  long-held position out of every valuation before it. `MERGER`/`ISSUECHANGE` are left out
  because either can be recorded against the position being *replaced* rather than the one
  received, and the row does not say which side it is.
- **Every ambiguity resolves to not flooring**, because not flooring only leaves the existing
  warning in place while flooring wrongly deletes a holding from its own history. Hence
  `quantity > 0` (a parent-side spinoff row carries no quantity change, and flooring *SPGI*
  is the catastrophic direction) and the requirement that the action account for the whole
  position at that date (a distribution of something already held must not floor the shares
  held before it). A spinoff whose `<CorporateActions>` row was never ingested keeps the old,
  loud behaviour — the floor is driven by recorded fact, and no record is no licence to drop
  a holding quietly.

It reaches `_calculate_timeline_swept`, `_calculate_daily_value` and
`holdings_snapshot_as_of` — the last because a 31 December before the spinoff was serving a
Steuerwert badged *partial* over a holding that did not exist. `/api/portfolio/attribution`
deliberately still excludes-and-counts it: a line that did not exist at the window start has
no start value to attribute against, and combining parent and child is a larger change than
this. Tests: `tests/test_spinoff_position_start.py`, plus the floor case in
`tests/test_timeline_equivalence.py` — the two walks must skip identically or the chart and
every point query disagree about the same day.

**Realized proceeds are inflows, not absences.** `calculate_xirr()` books each lot closed in the window
as a `+proceeds` flow (plus net dividends, era-spliced) alongside the `−cost` of lots opened. Without
that, selling A to buy B added a fresh outflow with no matching inflow, so every rotation crushed the
reported return — the planned IE→US ETF switch would have roughly halved it. Its guard is a
**sign change**, not `start_mv > 0 and end_mv > 0`: a window may legitimately start at zero (before the
first purchase) or end at zero (fully liquidated, where the proceeds carry the whole return). Windows
under 30 days return `method="simple_period"` and the UI labels that tile *Period Return* rather than
annualizing a few days of noise. Attribution takes the same disposal term
(`pnl = value_change + disposals − new_investment`), without which a position sold at a profit read as
`−start_value`. Tests: `tests/test_xirr.py`, `tests/test_attribution.py`.

**The timeline is swept once, not rebuilt per day.** `_calculate_timeline_swept()` folds each lot's
date-independent parts (base-converted cost at `open_date`, quantity) into running sums via open/close
events, so a day prices each *security* once instead of once per lot — O(days × securities) rather than
O(days × lots) with a 15-probe price walk inside. `_calculate_daily_value()` remains for point queries.
The two are numerically identical by construction and pinned that way
(`tests/test_timeline_equivalence.py` asserts exact equality across closed lots, a same-day rotation,
price gaps, a USD security and a CHF base) — **keep them in lockstep**.

**A split also invalidates the cached prices.** Yahoo restates historical `Close` after a split, but
`get_missing_dates()` only fetches dates we *don't* have, so pre-split rows are never refreshed while
IBKR restates the lot quantity immediately — leaving a step change in the chart that nothing detects.
So `invalidate_prices_for_splits()` deletes `market_prices` up to and including the action date
(`MarketPriceRepository.delete_up_to`), and the next market-data sync refetches them — one extra
request per security, since fetching is range-based.

**Holidays are not "missing" forever.** `get_missing_dates()` skipped weekends but not market holidays,
so a date the exchange never traded (4 July, Good Friday) stayed missing permanently — and one such date
makes `fetch_and_cache_prices` re-request that security's **entire range** on every one of the
daily jobs, indefinitely, against an IP-based rate limit. An **interior** weekday hole (cached data on
both sides) older than `HOLIDAY_GRACE_DAYS` (30) now counts as a holiday: every sync since has already
failed to fill it. Younger holes stay missing so late data can arrive, and **leading/trailing gaps stay
missing at any age** — which is exactly what a purge-and-refill repair looks like, so `--purge-prices`
and the split invalidation above still heal normally. `BenchmarkService` applies the same rule via its
own `_missing_business_days()`, shared by its price *and* FX paths. Tests:
`tests/test_missing_dates_holidays.py`, `tests/test_benchmark_fx_window.py`.

**A recent close is provisional, so it is re-fetched even when cached.** Yahoo answers a daily-interval
request with a bar for the session *in progress*, whose `Close` is merely the last trade so far — and
because `get_missing_dates()` returned only dates with **no row at all**, whichever job wrote a date
first owned it permanently. Read off `market_prices.created_at` on 2026-08-04: every European close in
production was its **15:00 Berlin mid-session price** (Xetra and Euronext run to 17:30), and Korea's
alternated between mid-session and final depending on whether the 08:00 or the 15:00 job happened to
land the row first. So a weekday within `PROVISIONAL_PRICE_DAYS` (3) of today is reported missing even
when cached, and `bulk_create`'s `ON CONFLICT DO UPDATE` lets the settled close overwrite it.

Three is what covers the two cases "today" does not: the 22:00 slot writes a US close within seconds
of the bell and only the next morning settles it, and **Friday's close must still be refreshable on
Monday**. It cannot collide with the holiday rule above — that needs a date older than 30 days and this
one needs it newer than 3 — and it cannot re-request forever, because a date Yahoo has no bar for ages
out of the window and falls back under the holiday rule. It costs **no extra requests**, only a wider
range on one already being made.

**`market_prices.created_at` records the first insert and is NOT bumped by a restatement**, because
`bulk_create` only updates the columns the caller supplies and the price dicts carry no `created_at`.
That is right for a column with that name, and it is a trap worth knowing twice over: it is the column
that *proved* the freeze (a row dated today, written at 13:00 UTC while Xetra had 2.5 hours to run),
and it is useless for confirming the fix — a restated row still reads 13:00. Verify a restatement by
the **price** changing, not by the timestamp. Nothing records when a price was last rewritten; an
`updated_at` column would, and would need a migration.

This is the **precondition for the intraday market-data slots**, not a separate cleanup: adding an
earlier slot on top of the freeze pins an *earlier* price. If the refresh is ever removed the slots
have to go with it. `BenchmarkService` opts in per caller — the read path yes, the scheduled warm-up no
— for the Yahoo-budget reason given in *Sync schedule*, and the **FX path deliberately never** does: an
ECB reference rate is published once a day, so there is no intraday rate to converge on, and
`_batch_fetch_rates` dedups per row and could not rewrite it anyway. Tests:
`tests/test_provisional_price_refresh.py`.

The fetch is also **span-narrowed**: the Yahoo request starts a few days before `min(missing)`
(`PRICE_FETCH_BUFFER_DAYS`), not at the window start — the 08:00 730-day job used to re-download two
years per security every morning purely because today's close hadn't published (~19.5k rows rewritten
to gain a few hundred). A split purge or a newly added security still pulls the whole span, because
that is where `min(missing)` then sits.

Two details carry the weight. `PRICE_RESTATING_ACTIONS` is a deliberate **subset** of
`SPLIT_LIKE_ACTIONS`: we fetch with `auto_adjust=False` and Yahoo rebases raw `Close` for splits only,
so `SPINOFF`/`STOCKDIV`/`ISSUECHANGE` are excluded as pure churn. And it fires **only for actions
newly inserted on this sync** (`CorporateActionRepository.existing_ib_keys()`) — the statement
resends every action inside its period every time, so without that check every daily job would wipe and refetch the
same history forever. Reported in `warnings[]` and as `prices_invalidated`.
Limitation: the 7-day jobs restore the current value, but the full history only comes back at the next
**08:00** 730-day `full_sync`. (There are six 7-day jobs now, not two, so the current value returns
within a couple of hours rather than by the evening.) Tests: `tests/test_split_price_invalidation.py`.

---
