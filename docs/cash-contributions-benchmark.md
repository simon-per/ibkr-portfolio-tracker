# Cash, money in, contributions, the benchmark

> Moved verbatim from `CLAUDE.md` on 2026-09-08, when that file became a short index so a
> session no longer loads ~92k tokens of documentation to start. **This file is
> authoritative for its subsystem**: every invariant here was a bug first. Keep it accurate
> the way CLAUDE.md was kept — update it in the same change that moves the code it describes.

## Cash — the balance a sale becomes

`cash_eur` / `total_value_eur` / `money_in_eur` on `/api/portfolio/value-over-time`,
`total_cash_eur` / `total_value_eur` on `/api/portfolio/summary`, a `Cash` bucket on
`/api/allocation/portfolio`, and `CashService` behind all of them.

**It exists because the app valued an account at the market value of its holdings and
called that the portfolio.** Read off production on 2026-08-26: a restructuring sold
25,136 CHF of positions on 08-21 and redeployed 12,682 of them on 08-24, so holdings went
68,342 → 43,631 → 56,161 and the value chart drew a 36% cliff followed by a partial
recovery — over a period in which the account lost nothing. The headline card reported
56,708 CHF for an account worth 68,921, an **18% understatement**, and every weight,
slice and "% of portfolio" divided by that short total.

Note what was *not* wrong, because it decides the scope: the **risk metrics were already
correct**. `dailyReturnSeries` nets `external_flow_eur` out of every pair, so 08-21 reads
**+0.76%** and the flow-adjusted max drawdown over the year is −11.49% from March. A sale
was never a fabricated loss in the statistics — only in the picture and in the totals. So
this feature deliberately **does not touch returns**: XIRR, Modified Dietz and the
monthly heatmap still measure the holdings, and a trade is still an external flow to
them. Making cash part of the measured pot (so only deposits count as flows) is the
textbook definition and a genuinely better one — it is left undone on purpose, not
missed.

### Derived from what moved, never from what is held

    cash = Σ trades (proceeds + commission)
         + Σ cash flows (deposits, withdrawals, cash legs of transfers)
         + Σ IBKR dividend payments, net of withholding

anchored at **zero before the first record**, which is definitional rather than an
assumption: the account did not exist.

**Deriving it from trades rather than from tax-lot events is the whole design, and it is
what makes that anchor safe.** This account's holdings arrived by in-kind transfer
carrying their *original* open dates and cost bases, back to 2024 — so a lot-event
derivation reads years of pre-IBKR purchases as cash draining an account that had not
been opened, and goes tens of thousands negative. A transferred lot has **no trade**, so
it correctly costs nothing, and the two other ledgers begin when the account does. That
is also why this is **not** spliced at `coverage_from` the way `get_contributions` is:
the splice exists because *lot cost basis* cannot survive a rotation, and cash is not
built from lot cost basis.

Four rules, each of which would be a wrong number the other way:

- **Every cash-flow type counts, not `get_deposits()`'s whitelist.** That one answers
  "was money *added*", where a transfer must never count; this asks "did cash *move*",
  where a transfer's cash leg genuinely did. The in-kind rows carry a zero amount, so
  including them is correct rather than merely harmless.
- **Dividends are IBKR rows only, and this is the one reader that does not splice.**
  `_splice_by_era` answers "how much income was earned", for which a pre-ledger estimate
  is the only evidence there is. A balance asks "how much cash arrived *at this broker*",
  where an estimate is evidence of nothing — it is a guess about a payment made into a
  Trading 212 account that IBKR's cash never saw. The rule lives in
  `DividendService.ibkr_cash_receipts` rather than in `CashService`, beside every other
  rule about those rows, because a service reaching into the columns itself is what
  `test_era_splice_boundary` exists to catch.
- **Each event converts at its own date**, matching how the timeline converts a lot's
  cost at its `open_date`. The two lines are on one chart and have to agree about which
  day's rate applies to a franc.
- **`unknown` is not zero.** No ledger row at all means there is nothing to derive from,
  and `cash_source` says so; a *derived* zero is a real answer for a fully deployed
  account. Collapsing them renders "we have no idea" as "you hold no cash" — the
  reassuring-zero failure this file keeps rediscovering, and `cashIsTracked` in
  `lib/portfolioCash.ts` is where the client refuses it.

**What it cannot see, which is why it is badged.** Broker interest, account fees (the
sanitizer already drops a `type="AF"` cash transaction) and the spread on FX conversions
appear in none of the three ledgers. They accumulate in one direction: on this account
the derived balance sat about **−250 CHF** for months while it was otherwise fully
deployed, which is 0.36% and is what a small persistent debit looks like. So `cash_source`
is `derived` and the UI says so in prose. Two independent derivations agreeing is the
reason to trust the size of it — the ledger sum gives 12,228.74 CHF and summing the
timeline's own `external_flow_eur` from `coverage_from` gives 12,212, a tenth of a
percent apart.

### An FX conversion is cash-to-cash, and idle foreign cash is a known soft spot

**IBKR books every currency conversion as a `<Trade>` with `assetCategory="CASH"`, and
`extract_trades` filters to `STK`** — so all of them are excluded from the trades ledger
and therefore from the derived balance. That is correct rather than lucky: a conversion
gives up one currency and receives another, so in *base* terms it nets to zero and adding
it would double-count the cash it moves. The 2026-08-26 statement carried **221** of them
against 37 STK trades, mostly IBKR's automatic per-trade handling (100 `USD.TWD`, 66
`USD.CHF`); exactly one carried a fee, −1.60, which is the deliberate bulk convert.

**What this does expose is a soft spot in the derived balance.** Cash events are
projected at *their own date*, which is right for the deployed-capital line they sit
beside — but an **idle balance held in a foreign currency really does revalue every day**,
and the derived series cannot see that. When the Ireland-to-US rotation left five figures
sitting in USD, that stopped being theoretical.

It is bounded rather than fixed, and knowing why is the point:

- **A measured row resets it.** Every successful Flex sync writes one, so the unrevalued
  window is normally a day. Two days of USDCHF on a 12,500 balance is about ±40 CHF.
- **The measured figure itself is already right**, because IBKR revalues into its own base
  before reporting `endingCash`. So whenever `cash_source` reads `ibkr` for a date, the
  currency mix is handled exactly.
- **It only grows when syncs fail**, which is the same failure `find_flex_generation_gap`
  already alarms on.

The clean upgrade, if it ever matters: tick **Currency Breakout** beside Base Currency
Summary. `resolve_cash_balances` already prefers the summary and would keep doing so, but
the per-currency rows would give a future revision the balances to revalue daily. Not
built, because a sub-half-percent correction on one line is not worth a second cash schema.

### `<EquitySummaryInBase>` is the measured answer, and it is off by default

`extract_equity_summary` reads IBKR's own end-of-day `cash` / `stock` / `total` into
`cash_balances`, and `extract_cash_report` reads `<CashReport>`'s `endingCash` for the
same table. Either section will do and **both are off by default**; until one is enabled
the table is empty, and that is a supported state rather than a pending migration.

`resolve_cash_balances` collapses the two schemas onto one row-per-date shape, so
`CashService` reads one thing. **The daily series wins on any date both cover** — they
describe the same quantity and should agree, and deterministic precedence is what stops a
re-sync flipping a stored value depending on write order. A Cash Report **Currency
Breakout** is converted to EUR at the report date and summed; an unconvertible currency
abandons that date rather than storing a partial total, because a balance short by one
currency is a plausible figure and it would overwrite a derived one that is already
better. A **Base Currency Summary** row is taken as-is and beats its own breakout, since
summing both doubles the balance — the one arithmetic error here that would look entirely
plausible on screen.

**A base-summary row says how much and never in what, so the base currency has to come
from elsewhere — and `AccountInformation` is an optional section this account does not
have enabled.** `IBKRService._base_currency` reads it where it exists and otherwise takes
the unanimous `toCurrency` off `<ConversionRates>`, which the query already emits (1,014
rows on the 2026-08-26 statement, every one `CHF`). **Unanimity, not a majority**: the
section's premise is that one currency is the base, so a split answer means the premise
is wrong. When neither source answers, the figure is **dropped rather than labelled** —
read as EUR, a CHF balance is ~7% high after projection, and it would overwrite a derived
balance that was already right to within a couple of percent. Guarded at both the ingest
and the reader, because rows written before the refusal existed still reach the reader.

**And a balance already in the display currency is not round-tripped through EUR.**
`NativeToBase.convert` short-circuits when the row's currency equals the base, because
the stored native→EUR and EUR→base rates are not exact inverses: 12,501.58 CHF came back
as 12,502.03 under a CHF base. Four significant figures in, on the one number in this app
people reconcile against a broker statement line by line.

`_apply_measured` splices them in as *corrections*: a measured row is a level while the
timeline sweeps deltas, so each becomes `measured − derived-so-far`. That keeps the whole
thing one sorted event list, which is what stops the chart, the summary card and the
allocation denominator from each deriving a balance. Corrections are **interleaved**
rather than replacing the derived era — carrying the last measured level flat across a
weekend or a failed-sync stretch would freeze the balance through real trades. Expect a
small one-off step on the first measured day: that is the accumulated interest and fees
becoming visible, not a fault.

**`cash_source` is per point, not per response.** The Flex window is bounded, so measured
history starts whenever the section was enabled and can never reach the account's start;
stamping `ibkr` on the years before it because the tail is measured is the same overclaim
as a badge that cannot clear. Hence `derived_source()` alongside `cash_source()` — the
first is the fallback for pre-measurement days, and using the second there was a real bug
caught before it shipped. **The measured era's label is `cash_source()`'s verdict too, never
a literal.** Until 2026-09-12 the timeline stamped `"ibkr"` on every point after the first
measured balance while `cash_source()` — read by the summary card and the allocation tab — said
`mixed`, because the pillar-3a account's cash is derived and IBKR never saw it. Live: 14 tail
points `ibkr`, the card `mixed`, and the chart's caveat gone, since it reads the last point.
`_calculate_timeline_swept` takes the label as `cash_measured_source`; pinned in
`tests/test_cash_balance.py`.

### Total Value vs Money In — the only pairing a rotation cannot step

The chart's lines change meaning when cash is tracked: **Total Value** (holdings + cash)
against **Money In** (cumulative contributions), with Profit/Loss as the gap. That gap is
*total* profit — realized, unrealized and dividends — rather than the unrealized-only
figure the old pair produced.

It is the only pairing with no step on a trade, and the two rejected alternatives say
why. Keeping **Market Value vs Cost Basis** and adding cash as a third line leaves the
cliff exactly where it was. Pairing total value with **cost + cash** removes the value
cliff but makes the *baseline* step up by the realized gain on every sale (+6.1k here),
because cost basis falls by what a lot cost while cash rises by what it sold for. Money
In steps only when money actually goes in.

**`money_in_eur` is the contributions splice, served daily.** It reuses
`_contribution_inputs`, which `get_contributions` also consumes — one event list, so the
strip's all-time figure and the chart's last point cannot disagree. Summing those events
over any window reproduces all three branches the strip used to compute inline
(`deployed`, `deposits`, `spliced`), because the two ranges neither overlap nor leave a
hole. Pinned by `test_the_chart_and_the_strip_agree_about_money_in` — this app has
already published two numbers under one name on one screen twice.

When cash is not tracked the chart falls back to the old pair, labels included. Absent
means "this backend does not track cash", never "cash is zero" — the same
backward-compatible reading `unpriced_holdings` and `external_flow_eur` both make.

### The benchmark invests contributions, not tax lots

`calculate_benchmark_value_over_time` buys hypothetical index shares from the **same
`money_in_legs`** the chart draws, so the comparison line and the baseline it is measured
against are one series rather than two.

**It was driven off tax lots until 2026-08-26, and that was wrong twice over.** A lot's
`close_date` emitted `-shares` — unwinding the position at the *number of shares bought*
while also removing its cost, so the gain those shares had accumulated simply vanished.
Read off production for the 08-21 restructuring, S&P 500 in CHF: `61,654 → 38,766 →
51,680`, never recovering. **4,193 CHF of gain destroyed by a day on which no money left
the account.** And it cliffed on a chart whose portfolio line no longer does, so the
picture read as a large outperformance that was pure artefact.

Contributions are rotation-neutral by construction — selling one holding to buy another
is not a contribution — which is exactly the property that makes them the right partner
for `total_value_eur`. A **negative** leg (a withdrawal) sells shares at that day's
price, because the money really did leave and the hypothetical has to fund it too.

Two consequences worth knowing:

- **Before `coverage_from` it still moves on a rotation, and that is honest rather than
  broken.** That era has no deposit ledger, so lot cost basis is the only signal and it
  cannot survive a rotation — the same limitation `get_contributions` reports as
  `money_in_method: "deployed"`. The benchmark inherits it instead of inventing a better
  answer. Pinned from both sides in `tests/test_benchmark_basis.py`.
- **There is no cached benchmark series any more.** `benchmark_timeline_cache` held the
  inception series and was sound only *given a fixed basis*, so three call sites cleared it
  (every Flex ingest, every finpension import, daily for the trailing week); once the chart
  moved to `anchor=window` on 2026-09-07 nothing read it at all, and it was retired on
  2026-09-08 (migration `u4d1f8a5b9c0`). Both anchors are computed on request — the walk is
  O(days) over preloaded prices and the provider fetches never went through the cache.

A gap remains, and it is CLAUDE.md's *dominant failure mode* in its mildest form:
`_apply_base_currency` converts the benchmark's running baseline at **each point's
date**, while the portfolio converts each leg at **its own date**. Same legs, two
projection rules, so under a non-EUR base the benchmark's baseline **wobbles with FX**
around a portfolio figure that sits still — measured across the week this shipped, 53,308
to 53,725 against a flat 53,330, so under half a percent either way. It is invisible on
the chart, because only the benchmark's *value* line is drawn, and it does not reach beta,
which excludes flow days. Recorded rather than fixed here; see *Worth doing next* in
STATUS.md.

**And note how the size of that gap was got wrong once.**
`calculate_benchmark_value_over_time` **already applies `_apply_base_currency` before
returning**, so a verification script that helpfully called it again measured a
double-converted series and reported the gap as 3,000 CHF rather than 250 — roughly one
EUR/CHF factor, which is exactly what a plausible wrong number looks like. Do not
re-project the return value; it is already in the base currency.

### The benchmark is anchored to the window, and its seed is the chart's own first point

`GET /api/portfolio/benchmark?anchor=window` (2026-09-07) is what the chart asks for on every
range. The since-inception series stays behind `anchor=inception` — the default, so an unknown
caller sees no change — and is computed on request like the window series.

Both lines used to be absolute: the portfolio's first point was its own value that day while
the benchmark's carried every contribution since 2024-05-28, so on 3M the starting gap was two
years of relative performance on a chart that drew none of it. Window mode answers a different
question — *what if, on the first day of this range, I had moved everything into the index and
then made the same contributions since* — and four rules carry it, each a wrong number the
other way:

- **The seed is Total Value (holdings + cash) on the anchor day, read through
  `get_portfolio_value_over_time(anchor, anchor)`** — the pipeline that draws the chart's first
  point, so the two lines start at one figure by construction rather than by a second
  valuation. Cash is in because the benchmark is drawn beside Total Value and invests the same
  Money In legs: the hypothetical is swapping the *whole account* into the index, so idle cash
  shows as underperformance against it, which is the honest reading. When cash is not tracked
  the balance is 0 and Total Value equals Market Value, so one rule covers both modes.
- **Legs strictly after the anchor buy or sell shares; a leg on the anchor day is inside the
  seed.** The portfolio's anchor point already contains that day's purchases (`events <= d`),
  so the window is `(anchor, end]`, the one `calculate_xirr` and `/attribution` use.
- **It is a seeded walk, never a scale or a shift.** Scaling the absolute series so its first
  point matched would scale the in-window deposits too, and the portfolio would then appear to
  outperform by exactly what was paid in. `_share_events_for_legs` and `_walk` are one
  implementation for both anchors; window mode passes a seed and the legs after it.
- **The seed is converted back to EUR through the anchor-day base rate**, because
  `_apply_base_currency` re-multiplies at that date. Seeding from the EUR components misses by
  the FX projection on cash — the timeline projects each cash event at its *own* date — by 75
  on a 2,490 seed in the test that pins it.

**Neither anchor is cached.** Window mode never was — a per-window series in a table keyed on
(benchmark, date) would have poisoned every other range — and the inception cache it bypassed
was retired the day after it shipped, since nothing read it any more. The walk over one window
is cheap; the expensive steps were always the provider fetches, which run exactly as before,
over the window only.

**ALL is a near no-op, measured.** The seed on the inception day is the first lot at that
day's close against a first leg at its cost, a fraction of a percent apart; two years later
the two series differ by 0.0011%. Do not special-case ALL back to the inception series — one
meaning on screen is the point, and the prose under the toggles names the anchor day.

**Beta is unchanged on flow-free days, exactly**: between contributions the share count is
constant under either anchor, so the day-over-day ratio is the index's own return. What *did*
move was a pre-existing contamination. A deposit into cash is not a flow to the holdings, so
the portfolio's `external_flow_eur` read 0 and `betaAndCorrelation` kept the day — while the
hypothetical bought index shares with it and its ratio jumped by deposit ÷ value (+1,002 CHF
on 2026-08-26, a +1.7% "benchmark return" against a portfolio that did not move; three such
days in one 3M window). Window-mode points now carry `external_flow_eur` — the contributions
applied that day — and beta skips a day whose benchmark point carries a non-zero **explicit**
flow. Explicit only: the cost-line inference `externalFlow()` falls back to is what measured
the exchange rate and left 9 usable days out of 147 in August. Inception-mode points report
`None`, never 0, because the cached rows carry no flow and a series reporting it on its fresh
points only would disagree with itself.

**An anchor the portfolio could not fully price is served and declared**, not refused —
refusing would silently flip the line's meaning back to inception. `anchor_unpriced_holdings`
rides on the response and the chart folds it into its incomplete-valuation notice: the seed is
understated, so every point after it is. The rebased `cost_basis_eur` (seed plus contributions
since) inherits the FX wobble recorded under *Worth doing next* in STATUS.md; it is not drawn.
Tests: `tests/test_benchmark_window_anchor.py`, the anchor cases in
`PortfolioValueChart.test.tsx`, the contribution-day cases in `portfolioKpis.test.ts`.

### Where cash reaches, and where it deliberately does not

- **The value chart and the summary hero card.** The card names the split in its footnote
  (`X in holdings + Y cash`) rather than growing a sixth tile.
- **Positions weights**, whose denominator is now holdings + cash. The column is labelled
  "% of portfolio" and cash is part of one; dividing by holdings alone inflated every row
  by 21% here. Cash is a line above the table rather than a synthetic `Position` row.
- **All three allocation breakdowns**, as a `Cash` bucket. In *all three* on purpose: a
  cash slice in the asset-type chart alone would give the endpoint two denominators and
  leave sector and geography summing to under 100 while every slice still said "% of
  portfolio" — the exact failure that tab was fixed for in August, arriving by a new
  route. A **negative** balance is excluded from both the slice and the denominator and
  reported as `cash_eur`, because a pie cannot draw a negative slice and renormalising
  around one inflates every other holding.
- **Not returns** — XIRR, Modified Dietz, the monthly heatmap, drawdown, Sharpe, beta.
  See the top of this section: they were already right, and changing what counts as a
  flow is a separate decision.
- **Not the dividend yield or the rebalance drift denominators.** Both would be
  defensible (a yield on total value; drift against the capital you actually have to
  allocate) and both were explicitly left out of scope.

Tests: `tests/test_cash_balance.py`, the cash block in `tests/test_api_smoke.py`,
`src/lib/portfolioCash.test.ts`.

---

## Contributions — money in per month

`GET /api/portfolio/contributions` → `PortfolioService.get_contributions()`, rendered as a slim strip
(`ContributionsStrip.tsx`) below the KPI cards: all time / 12M / 6M / 3M, each trailing window shown as
a delta against the all-time average, so a change in savings rate is visible at a glance.

**`money_in_eur` is the answer**, and it is **spliced at `coverage_from`** because no single source is
authoritative for the whole history. This design took three attempts; the reasoning below is why.

### The splice

| Era | Source |
|---|---|
| before `coverage_from` | **lot cost basis** (`Σ cost_basis_eur` of lots opened) |
| from `coverage_from` | **real deposits** (`cash_flows`, `DEPOSITWITHDRAW` only) |

`money_in_method` reports which applied: `deposits` \| `spliced` \| `deployed`.

Lot cost basis reaches back through the pre-IBKR years because the early-2026 portfolio transfer from
Trading 212, Scalable Capital and Trade Republic carried every lot across with its **original `openDateTime` and original
cost basis** — verified, not assumed: securities have as many distinct `costBasisPrice` values as they
have lots (DBPG 75 lots / 72 prices, XNAS 110/107, XAIX 54/54), which a transfer-date re-basing could not
produce. Lots then survive indefinitely because reconciliation deletes **open** lots only and splits a
partial sale **pro-rata** under the original `open_date`.

Deposits take over the moment a ledger exists, because **lot cost basis cannot survive a rotation**:
selling one ETF to buy another closes lots and opens new ones for the same money, so it is counted twice.
That is not hypothetical — the Ireland-domiciled sleeve is being switched to US-domiciled ETFs for tax
reasons. Simulated on the real lot set (every EUR lot rotated into a USD one, no new cash), `money_in`
held **exactly flat in all four windows** while deployment roughly **doubled** all-time and went up
**~4x** over 3M — the shorter the window, the worse the distortion, because the rotation fills more of
it. A deployment-led headline would have claimed four times the real 3-month contribution. Pinned by
`test_a_rotation_does_not_inflate_money_in`.

**No double-count at the boundary**, because it is a single date: lots are summed strictly `< coverage_from`
and deposits strictly `>= coverage_from`. A purchase funded by a deposit in the boundary month contributes
the deposit only. The two ranges also leave no hole, which is why the divisor is the window's **full**
elapsed months and every window carries a meaningful number.

`coverage_from` lives in `app_settings` (`cash_flows_covered_from`), set from the statement's `from_date`
and **only when deposit rows were actually present** — an export taken without the Deposits option must
not claim coverage it has no data for. It only ever widens backwards
(`widen_cash_flows_covered_from`), so neither a later statement nor the 2026-07-31 switch to a
30-day window can shrink what a prior-year import established — the narrower `from_date` is simply
ignored, which is what kept `coverage_from` at 2026-01-09 through that change. It must be the period start, not the first deposit's date: a covered week with no deposits is
still covered, and using the first row would hand that week's purchases to the lot side *and* count its
deposits.

**But the period start is a claim, not evidence, and `get_contributions()` clamps it forward to
`CashFlowRepository.earliest_flow_date()`** — the first row the ledger holds, of *any* type. **The account
is younger than the statement that reports it**: a YTD query in the first year begins on 1 January while
the account was funded weeks later, and in that gap the deposits table is empty because the money was
still going to the previous broker. Believing the claim drops those purchases from **both** sides — past
the lot cutoff, with no deposit standing in for them — so they vanish from money in with nothing
reporting it. Clamping hands the gap back to lot cost basis, which is the right source for any era the
ledger doesn't reach.

Two details. It keys on the earliest row of **any** type, not the earliest deposit: an account opened by
an in-kind transfer can trade before any cash is deposited, and anchoring on the first deposit would
leave that window on the lot side where a rotation inflates it. A transfer is never money in, but it *is*
evidence the account existed. And the clamp is applied at **read** time rather than stored, so it needs no
migration, a later YTD sync can't undo it, and a prior-year import can't silently move the boundary
back into an era the ledger has nothing for. (No such import is planned any more — the 2025 backfill
was closed on 2026-08-17 because IBKR holds nothing from before 2026 for this account. The clamp still
earns its place: it is what covers the weeks of January 2026 before the ledger's first row.)

Do **not** "simplify" this by splicing at the transfer date instead. Deposits into the new account
routinely start *before* the positions arrive, and those deposits fund purchases made after it; a
transfer-date boundary drops them from the deposit side while their lots sit past the lot cutoff. On this
account that is the larger error of the two. Tests:
`test_coverage_cannot_start_before_the_ledger_has_any_row`,
`test_a_transfer_row_alone_anchors_the_ledger_start`.

### `deployed_eur` — secondary, and deliberately still shown

Cost basis of lots opened, the old headline. Once rotation starts it exceeds `money_in_eur`, and **that gap
is the useful part**: it is capital churn, not saving.

**Do not promote it back, and do not "fix" it by averaging `net_eur` instead.** Both were tried. Averaging
net moves the error rather than removing it — a window then gets debited for a sale of something bought
*before* it began. The two are duals; `net_eur` survives only for the tooltip and the identity check.

**But "secondary" has to be true on the screen too, and until 2026-09-06 it was not.** `monthly[]` carried
`deployed_eur` and `net_eur` and **no money in at all**, so the one card whose subject is contributions
could only ever draw the gross series. Measured on production the month the Ireland→US ETF rotation
landed: **August 2026 deployed 30,616.82 CHF against 7,211.40 of real money in**, drawn as a bar four
times the height of any contribution this account has made — and **September read 3,638.82 deployed
against 0.00 in**, a headline figure for a month in which nothing was paid at all. Both numbers were
correct and neither was the answer to the question the card asks.

So the series carries `money_in_eur` first, summed from the same `money_in_legs` — no second splice; this
is that rule's **third** reader after the strip's windows and the chart's daily line, and
`Σ monthly[].money_in_eur == windows['all'].money_in_eur` is the identity that keeps them honest
(`test_api_smoke.py`, and all three pinned together in `test_cash_balance.py`). `MonthlyDeploymentCard`
draws money in as its bar, drops `deployed_eur` and `net_eur` to the tooltip where the latter's own
docstring already said it belonged, and names the largest rotation month in prose under the chart — the
**largest** rather than the latest, so the note cannot vanish while the spike it explains is still on
screen.

**"Secondary" became "off the surface" on 2026-09-07, one day later, and the reasoning is worth keeping
because it is not the reasoning above.** Deployed shipped as a *second bar* beside money in, and as the
strip's muted `/suffix`, on the argument that the gap is capital churn and should be readable without
hovering. The account owner asked for both out: they read these two surfaces for one thing — how much
goes in per month — and a second figure sharing the slash with it, one a rotation can quadruple, made the
one they wanted harder to read. It was also mostly redundant ink, which the data settles rather than
taste: **the two series are the identical number in 20 of this account's 29 months** — every month before
`coverage_from`, where money in *is* lot cost basis — and differ by a few hundred francs of dividend
reinvestment in five more. It earned its ink in two months out of 29, and those two are exactly what the
prose note carries.

**Do not read that as licence to delete `deployed_eur`.** It stays on the wire, in both tooltips, and in
the strip's hover `title`, because it is the only *independent* check on the highest-risk failure in this
feature: money in comes from the deposit ledger and deployed from tax lots, so a broker transfer booked as
an ordinary deposit inflates money in with nothing else on screen able to disagree. Off the surface is not
gone. (This is also why the ~12% agreement between the two derivations recorded under *Current state* is
evidence rather than trivia.) `showDeployed` is now the legacy-backend fallback alone — an older backend
publishing no `money_in_eur` still gets the old deployment-only chart, because absent means "older
backend", never "nothing was paid in".

**The months are the union of both key sets, and that was a second bug in the same place.** The series was
keyed on months with *lot* activity, so **a month carrying a deposit and no purchase had no row** — the
contribution simply absent from a chart of contributions, with the sum quietly short of the window that
includes it and nothing saying so. Latent while lots came first (the in-kind transfer carried its 2024-25
open dates, so every deposit had lot activity around it) and live the moment a retirement account arrived,
which is the opposite shape: the pillar 3a deposits landed 2026-08-25 against purchases on 09-01. Both
monthly loops also clamp at `as_of`, as the windows already did, or the identity holds only when no row is
future-dated.

### Transfers are never money in

An incoming transfer moves capital saved years earlier somewhere else, and the transferred lots already
carry their own `open_date` — so counting it would both invent savings in a month that had none *and*
double-count purchases already recorded. `CashFlowRepository.get_deposits()` therefore selects
`flow_type == DEPOSITWITHDRAW` by **whitelist**, so no new transfer-ish type can leak in.

IBKR may book a transfer's cash leg as an ordinary "Deposits & Withdrawals" row. `persist_cash_flows()`
catches that by matching `(flow_date, amount, currency)` against the `<Transfers>` rows — exactly, never
on description text — and reclassifies, reporting it in `warnings[]`. Zero-cash (in-kind) transfers are
left out of the match keys, or every no-cash transfer would collide on `(date, 0)`.

**This is the highest-risk number in the feature**: an unexcluded transfer shows a portfolio-sized fake
contribution. `app/cli/manage_cash_flows.py` is the manual override — `list` marks which rows count as
added, `reclassify <ib_key> --as TRANSFER_IN` fixes one, `--dry-run` on the mutating path, and every
edit records a `sync_runs` row (`manual_cash_flow`).

Three things this account's real data settled, so nobody re-investigates them:

- **The 2026 transfer was entirely in-kind** — all 22 rows carry `cashTransfer=0`. So there is no
  transfer cash to misclassify and `deposits_reclassified_as_transfer` is legitimately **0**. Read a zero
  there as correct, not as the guard failing to fire.
- **`Transfer.type` arrives as `FOP`** (Free Of Payment), which ibflex's `TransferType` enum
  (`INTERNAL`/`ACATS`) cannot convert, so the sanitizer drops it and `transfer_type` is always
  `'UNKNOWN'` here. `_transfer_to_flow` leaves `UNKNOWN` out of the description. **Do not extend the
  enum** — same reasoning as everywhere else in the sanitizer.
- **`deliveringBroker` is not modelled by ibflex** either, and `company` comes through empty, so a
  transfer row cannot name Scalable Capital / Trading 212. `direction` *does* survive, which is what
  `TRANSFER_IN` and `earliest_transfer_in_date()` depend on.

### Currency

Every amount is stored EUR-converted at its own date (`cash_flows.amount_eur` via
`convert_to_eur(amount, currency, flow_date)`, `taxlots.cost_basis_eur` at `open_date`) and projected into
the base currency once at read time by `BaseFx` — deployment at each lot's `open_date`, deposits at each
flow's `flow_date`. A **zero amount skips conversion entirely**: zero is zero in every currency, and
demanding an FX rate would drop the in-kind transfer rows, which are exactly the ones with no cash. An
unconvertible non-zero currency skips that row with a warning rather than failing the sync.
`test_base_currency_projection_scales_both_metrics` pins the CHF path, since the tests otherwise run on EUR
while production runs on CHF.

### Shared mechanics

The divisor is **clamped to elapsed history** (`partial: true` when clamped), so a
four-month-old portfolio can't report a 12-month average divided by 12; all-time divides by exact days,
not whole months, so a part-month isn't rounded away. `as_of` is injectable purely so tests can pin the
windows. Cash-flow ingestion is a pure additive upsert with no delete, so it needs **no** empty-statement
wipe guard, and an unconvertible currency skips one row rather than failing the sync.

The cheap correctness check: `Σ monthly[].net_eur` must equal the current total cost basis, since every
lot is either still open or was released. **Exact in EUR; approximate once projected.** Each leg is
converted at its own date, so a lot bought and sold months apart contributes `+convert(cost, open_date)`
and `−convert(cost, close_date)` — which cancel to zero only if the rate didn't move. Under CHF the
residual is a fraction of a percent of *closed* cost basis and grows with FX drift, not with error. So
run the identity against `taxlots.cost_basis_eur` (`Σ` of open lots) when you want it to the cent, and
read a small non-zero gap in the base currency as FX, not as a dropped lot. Tests:
`tests/test_contributions.py`, `tests/test_cash_flow_ingest.py`.

---
