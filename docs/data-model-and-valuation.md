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
- **trades** — authoritative executions, idempotent on `ib_key`: `buy_sell` ∈ `BUY` | `SELL` |
  `CANCELBUY` | `CANCELSELL` (ibflex's member names; asked only through `app/models/trade.py`, see
  *A cancelled sale is no sale*), `quantity`, `price`, `proceeds`, `commission`, **`realized_pnl`**
  (IBKR's own FIFO). `security_id` is nullable — a fully sold security is no longer in OpenPositions.
- **corporate_actions** — `action_type` (Reorg name), `quantity`, `value`, `proceeds`, `description`.
- **cash_flows** — external cash, idempotent on `ib_key`: `flow_date`, **`flow_type`** ∈
  `DEPOSITWITHDRAW` | `TRANSFER_IN` | `TRANSFER_OUT` | `TRANSFER`, `amount` (signed as IBKR reports:
  deposit +, withdrawal −), `amount_eur` (pre-converted at `flow_date`). Only `DEPOSITWITHDRAW` counts as
  money added — see the contributions section.
- **dividend_payments** — `gross_amount_eur`, **`withholding_tax_eur`**, **`net_amount_eur`**,
  `pay_date`, **`source`** ∈ `ibkr` | `yfinance_estimate`. An estimate stores its gross as net; it is
  read through `IncomeSplit` (docs/dividends.md).
- **dividend_withholding_adjustments** — IBKR withholding lines posted on a day with no dividend of
  their security (refunds, corrections), idempotent on `ib_key` (IBKR transactionID):
  `posted_date`, `amount` (IBKR's sign), `withholding_eur` (+ withholds, − refunds) and
  `target_pay_date`, the IBKR dividend row it was folded into (NULL while none qualifies).
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

**Today's date is `app.clock.utc_today()`, never `date.today()`** (issue #45). `date.today()` is the
host's local date — the same missing-`TZ` dependency, a day ahead of UTC from 00:00 to 02:00 on a
Berlin machine in summer — and it sat at 34 sites beside `utcnow().date()` at seven, so one cutoff
could be built from two calendars. `tests/test_clock_convention.py` bans `date.today(` under `app/`;
the tests seed with `utc_today()` too, so they agree with the code around local midnight. A site that
genuinely means another calendar converts `utcnow()` into that zone explicitly — the ET day of a Flex
generation (`flex_generation.et_date`), the Berlin day the crypto FX warm-up is bounded in
(`crypto_service._berlin_day_start_utc`). None of the 34 replaced sites needed one: the closest,
`last_finished_session`, takes the UTC day because every benchmark's exchange (Tokyo to New York) has
closed by 24:00 UTC, so the weekday before the UTC day is final everywhere.

Migrations: `cd backend && alembic upgrade head` (the container CMD runs this on every start).

---

## Reconciliation & realized P&L

`reconcile_taxlots()` (`sync_helper.py`) first works out **which lots lost shares**, then explains
the loss for the security as a whole, in priority order:
1. **SELL trades** → close the shares those lots lost, stamped with the latest SELL date in the
   window; `close_source='trade'`. A SELL that IBKR cancelled inside the window is none
   (`uncancelled_sales`, issue #58)
2. **Corporate actions** → deterministic reclassification (split/spinoff/merger), not a sale
3. **Fallback heuristic** (quantity drop + `COST_CONSERVED_RATIO`) → `close_source='heuristic'`

**The diff is per lot, not per security** (GitHub issue #54, 2026-10-18). `_missing_per_lot`
measures, for each snapshot lot, the shares the statement no longer reports **on that lot's
`open_date`**: snapshot shares on the date minus incoming shares on it. Within one date it pairs
lots by `(open_date, price_per_unit)` first, so of two same-day lots the one IBKR stopped
reporting is the one that closes; lots with no price counterpart (spinoff drift moves every
price) absorb the rest in snapshot order. The open date is the identity because IBKR keeps it
through every restatement, while the price drifts and a split moves both quantity and price.
Until then Phase D compared the security's **net** quantity and closed FIFO, which broke two
ways. A **sale and a rebuy inside one window** (a failed slot, a lockout, the ET Sun/Mon skip, or
just a sale and a rebuy a day apart) netted to zero, so nothing closed — and Phase B had already
deleted the old lot, so every valuation before the rebuy held none of it: timeline, a past
31 December Steuerwert, XIRR, attribution, contributions, benchmark, with nothing counted
anywhere. And a **sale IBKR matched to a newer lot** (any lot-matching method but FIFO) closed
the oldest lot, which IBKR still reports open, so it existed twice and every date before the sale
held it double while the lot actually sold vanished. Open lots are still deleted and re-inserted
from the statement (nothing keys on `taxlots.id`, and it keeps Phase B one statement); only the
closures changed.

**Without a SELL on record, inference still needs the holding to have shrunk.** A lot that
changed shape while the net quantity did not fall — IBKR re-lotting, or a sale and rebuy with no
`<Trades>` to show for it — books no disposal, exactly as the net comparison concluded. So the
per-lot diff never records a sale on less evidence than before; a sale-and-rebuy is recorded only
where its SELL is in `trades`, which is every sync since `<Trades>` was enabled. Lots already lost
to the old rule before this change do not come back. Pinned in `tests/test_sync_helper.py`,
including the family property: across a reconcile the statement explains, **no date before the
window gains or loses shares** and the window's end holds what IBKR reports.

**That order is the code's order, and it wasn't until 2026-07-30.** The trade lookup used to run
*third*, supplying only a date and a provenance tag after 2 and 3 had already decided — so a trim of
**≤1% of cost basis** took the cost-conserved path and recorded **no closure at all** while IBKR's own
SELL sat in `trades` for that window (200 shares at 20,000 sold down by 2 leaves 19,800, and
`19800 >= 20000 * 0.99`). The disposal then never reached `calculate_xirr()`'s `+proceeds` inflow, the
attribution's disposal term, that day's `external_flow_eur`, or the tax report's `closed_lot_estimate`.
The two inferences now run **only when no SELL is on record**, so the reverse-split and cash-in-lieu
protections are untouched — pinned from both sides in `tests/test_sync_helper.py`. Both still
decide for the security as a whole: the cost-conservation ratio compares the security's total
incoming cost with its snapshot cost, not a lot's.

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
gains. Pinned by `tests/test_realized_totals.py`. Since issue #58 it keys on a sale that *stands*:
a SELL beside its CANCELSELL is no sale on record.

**A sale the realized totals leave out is counted, on either source** (GitHub issue #26). The trade
path drops a SELL with no FX rate near its trade date — until 2026-10-18 only a log line, so the
`/summary` realized headline understated with nothing to say so. Both paths now return
`unpriced_disposals`: the trade path its unconvertible SELLs, the closed-lot fallback the lots in
`last_realized_skipped` (below). `/summary` has no `warnings[]`, so the count is the surface; the
hero tags *Realized gain* with "N sales unpriced". Test: `tests/test_closed_positions_partial_fx.py`.

**A cancelled sale is no sale, and "is this a sale?" is asked in one place** (GitHub issue #58,
2026-10-18). When IBKR busts an execution it sends a second row, `buySell="SELL (Ca.)"` (stored as
`CANCELSELL`) or `"BUY (Ca.)"` (`CANCELBUY`). The question was written inline seven times on the
server — `(t.buy_sell or "").upper() == "SELL"` in the realized totals (twice), the tax report, the
closed positions, the activity ledger, reconcile and the restamp — and once more on the client as
`subtype.includes('SELL')`. No copy knew a cancellation, so a busted sale's P&L stayed in the
`/summary` headline, the tax report and the closed positions, reconcile closed lots with it, and
Activity counted the CANCELSELL as a sale while the server did not. Now `app/models/trade.py`
answers it: `trade_kind`, `sale_sign` (+1 sale, −1 its cancellation, 0 otherwise), `is_sale_side`,
`is_cancellation`, `uncancelled_sales`.

- **The sign convention is an assumption, stated.** A cancellation row carries its execution's
  amounts **negated** — a CANCELSELL has positive quantity, negative proceeds and the sale's
  `fifoPnlRealized` with its sign flipped — so the two rows sum to zero. That is IBKR's statement
  convention; ibflex parses the attributes and says nothing about their signs, so the code cannot
  verify it, and it is the assumption the derived cash balance already made (`CashService` sums
  `proceeds + commission` over every trade as stored). So a reader that **sums money** takes both
  rows as stored and the pair nets out by itself, in whichever window holds the two; a reader that
  **counts sales or needs a sale's date** uses `sale_sign` or `uncancelled_sales`, which strikes
  out the SELL each CANCELSELL reverses (same contract and absolute quantity, preferring the same
  date, then price, then the nearest date). A cancellation whose sale lies outside the caller's
  rows strikes out nothing. If IBKR were ever seen sending a cancellation with its original's
  signs, every sum here and the cash balance would be wrong together; that is the place to look.
- **The realized-from-SELL loop is `realized_trades.realized_from_sell_trades`, and there is one
  of it.** It had three copies with three failure policies. It converts every sale-side row
  (SELL and CANCELSELL) through `NativeToBase` and returns the converted rows, the unconvertible
  ones, and per-security sums with `net_sales` (sales less cancellations); a row converts when its
  proceeds *and* its realized figure do. Each reader keeps its own policy: the totals sum every
  converted row and count the rest in `unpriced_disposals`, falling back to the closed lots when
  no sale stands; the tax report lists the year's rows ([tax.md](tax.md)); the closed positions
  refuse a security whole unless every row converted and a sale stands
  ([performance-analytics.md](performance-analytics.md)). The one behaviour that moved for data
  without cancellations: the closed positions used to need only the *realized* figure to convert,
  so a sale with a realized P&L of exactly zero and no rate for its proceeds read as `trade`; it
  now falls back like any other unconvertible sale.
- **Reconcile and the restamp read `uncancelled_sales`.** A SELL busted inside the window no
  longer settles a drop as `close_source='trade'` on the busted date — the drop, if any, goes to
  inference — and a cancelled SELL is no restamp candidate.

`tests/test_trade_kind_family.py` fails any comparison against `buy_sell` (or a trade-kind literal
in a module that imports the trade model) outside `app/models/trade.py`, and any `.realized_pnl`
read outside `realized_trades.py`; it also runs a sale and its cancellation through every reader.
Reconcile and restamp cases are in `tests/test_sync_helper.py` and
`tests/test_trade_security_linking.py`.

**A lot sold on D is not held at D's close.** One convention, everywhere: `_calculate_daily_value`,
`holdings_snapshot_as_of()` and the attribution gates all exclude **on** the close date, matching what
the benchmark always did. Under the old include-on-close rule a same-day rotation double-counted that
day (the sold lot still "active" beside its replacement) and a position sold on 31 December landed in
both the year's Steuerwert *and* its realized gains. Consequence to expect: a sale's value leaves the
chart on the sale date itself, one day earlier than before. The disposal windows in `calculate_xirr()`
and the attribution endpoint are therefore `(start, end]` — a lot sold *on* the window end yields
proceeds precisely because the end valuation no longer carries it. Change these together.
Tests: `tests/test_close_date_boundary.py`.

**"Held at D" is written once** (GitHub issue #43, 2026-10-18): `security_repository.lot_held_at(lot,
d)` in Python and `security_repository.held_at(d)` — a criterion on `TaxLot` rows — in SQL, both
`open_date <= D < close_date` and pinned equal over D−1, D and D+1. `_calculate_daily_value`, the
attribution gates, `holdings_snapshot_as_of()`, the spinoff floor's quantity check and both dividend
share counts (*Shares held on the ex-date* in [dividends.md](dividends.md)) call them; the swept
timeline applies the same rule as a removal event on the close date and is pinned to
`_calculate_daily_value` by `tests/test_timeline_equivalence.py`. `tests/test_held_at_date_family.py`
fails on any other comparison against a `close_date` in `app/` unless it is listed with its reason (the
realized disposal window, the trade-to-lot restamp match). Its sibling for the security set is
`security_repository.held()` — "has an open lot" — which every held-securities reader uses;
`tests/test_held_predicate_family.py` fails on a hand-written `TaxLot.is_open == True` /
`close_date IS NULL` outside the repositories (GitHub issue #42).

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

**A disposal that cannot be priced is counted, not dropped** (GitHub issue #19).
`realized_rows_from_closed_lots()` leaves out a lot with no price (14-day lookback) or no FX rate
near its close date — a sold security whose price history has a gap of 15+ days right before the
sale. Until 2026-10-18 that was only a log line, so every window reader lost the sale's proceeds
and still called itself complete: attribution read `pnl = −start_value` with `unpriced_holdings: 0`
(the security is held at neither endpoint, so the endpoint checks never saw it), and XIRR read low
with `last_xirr_unpriced = 0`. The skipped lots now land in `PortfolioService.last_realized_skipped`
(security id → close dates), a per-run latch reset on entry like `last_snapshot_skipped` — a tuple
was rejected because six callers, the tax report and the tests unpack the rows. Read it right after
the call. Attribution adds the ids to its `unpriced` set (excluded from both sides and counted, so
the decomposition and segments follow), XIRR adds the count to `last_xirr_unpriced`, and the
timeline counts the security in `unpriced_holdings` on the row whose `external_flow_eur` is short
(a weekend close rides to the next weekday, as its flow does). Closed positions and the tax
estimate each carry a `warnings[]` line, and `get_realized_totals`' fallback counts them in
`unpriced_disposals` (since issue #26, above). Test: `tests/test_realized_skip_counted.py`.

**The attribution's per-security loop is `attribution_rows()` and there is one of it** (since
2026-09-13). `get_performance_attribution` presents it; `PerformanceAnalyticsService` splits it
into price and FX and folds it onto sectors. It keeps both base-currency and price-currency
Decimals so the split can be made downstream, and `realized_rows_from_closed_lots` carries
`proceeds_local` and `price_currency` for the same reason. Its `unpriced` set holds a security
unvaluable at an endpoint **or** sold inside the window with proceeds that could not be priced
(above). The convention for the split, and
why the local leg is `None` rather than 0 when an input is missing, are in
[performance-analytics.md](performance-analytics.md). Change the disposal window or the
close-date rule here and every consumer moves with it.

**The timeline is swept once, not rebuilt per day.** `_calculate_timeline_swept()` folds each lot's
date-independent parts (base-converted cost at `open_date`, quantity) into running sums via open/close
events, so a day prices each *security* once instead of once per lot — O(days × securities) rather than
O(days × lots) with a 15-probe price walk inside. `_calculate_daily_value()` remains for point queries.
The two are numerically identical by construction and pinned that way
(`tests/test_timeline_equivalence.py` asserts exact equality across closed lots, a same-day rotation,
price gaps, a USD security and a CHF base) — **keep them in lockstep**.

**Every percentage of a whole on these paths goes through `percentages.share_pct`, so a zero whole
is `None`, never 0%** (GitHub issue #29, 2026-10-18). Seven inline `x / whole * 100 if whole > 0
else 0` copies answered 0: the timeline point's and `_calculate_daily_value`'s (so `/summary`'s)
gain % before anything is held at cost, the position's gain % on a zero cost basis (a stock
distribution reads no percentage, not `+0.00%` over a positive gain), the attribution's
`weight_percent` when everything attributed ends the window at zero, its `contribution_percent`
when contributions cancel exactly (a +100 and a −100 both read 0.0%), and both benchmark point
paths. The empty-book summary sends `None` too. The attribution now works in Decimal: weights
divide the rows' `end_mv_base` by their Decimal sum, and contributions divide each published
two-decimal P&L by their exact Decimal sum, so a cancelling pair is an exact zero rather than a
float residue dividing into a huge share. One behaviour moved with the helper: `share_pct` tests
for a non-zero whole where the copies tested `> 0`, so a negative cost basis (a short lot) would
now produce a figure rather than 0. `tests/test_share_of_nothing_is_absent.py` pins
the cases; `tests/test_percentages_family.py` fails the inline form coming back anywhere under
`app/`.

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
and the split invalidation above still heal normally. A trailing gap that can never close — a
delisted ticker — is bounded by the price loop instead: a sold security leaves the intraday passes
after `SOLD_PRICE_GRACE_DAYS`, its held span is fetched only up to its last close, and an empty
answer from every provider backs it off for up to 30 days (`price_backoff_until`;
[flex-and-sync.md](flex-and-sync.md), *Sync schedule*). The one trailing gap forgiven is in that
repair probe only: a run of at most `REPAIR_TRAILING_GRACE_WEEKDAYS` at a close date older than
`HOLIDAY_GRACE_DAYS` (the close itself an exchange holiday or a delisting date; issue #33), via
`get_missing_dates(trailing_grace_weekdays=…)`, off by default. `BenchmarkService` applies the same rule via its
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
have to go with it. `BenchmarkService` opts in per caller — the read path yes, the scheduled warm-up no,
because it stops at the last finished session and so never writes a provisional row (*Sync schedule*)
— and the **FX path deliberately never** does: an
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
**18:00** 730-day `full_sync` (it moved there from 08:00 with the IBKR slot on 2026-08-08). (There are six 7-day jobs now, not two, so the current value returns
within a couple of hours rather than by the evening.) Tests: `tests/test_split_price_invalidation.py`.

---
