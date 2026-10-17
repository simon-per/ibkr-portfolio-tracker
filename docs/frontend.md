# Client-side analytics and the mobile layout

> Moved verbatim from `CLAUDE.md` on 2026-09-08, when that file became a short index so a
> session no longer loads ~92k tokens of documentation to start. **This file is
> authoritative for its subsystem**: every invariant here was a bug first. Keep it accurate
> the way CLAUDE.md was kept — update it in the same change that moves the code it describes.

## Client-side analytics — risk, targets, currency

Four pure `frontend/src/lib/` modules with no endpoint of their own: they compute from series and
positions the page has already fetched, which is why they add **no request and cannot reach Yahoo**.
`portfolioKpis.ts` feeds the Performance tab's two card rows; `rebalance.ts` and
`currencyExposure.ts` feed two panels on the Allocation tab; `forecast.ts` is the Forecast tab's
projection and the baseline it starts from.

**One rule spans all three, and it was a real bug before it was a rule.** `undefined` data means
*not loaded*; an empty array means *nothing held*. Collapsing them lets a panel build a confident
answer out of an outage — the rebalance panel did exactly that, reporting *0 positions outside the
band* above rows reading *Not currently held*, i.e. that nothing needed rebalancing and that held
positions were not held. Unit tests could not see it because the shape needs a saved target;
`e2e/errors.mjs` covers it now. Any new panel here takes `isError` and treats absent data as a
stated failure.

**The two KPI rows this module feeds did not follow it until 2026-08-05**, which is the variant worth
naming: `PerformanceMetricsCards` and `RiskMetricsCards` returned `null` whenever `metrics` was null,
and Dashboard's memos return null when their query fails — so a backend outage did not render an error
state on those rows, it rendered *nothing*, and twelve metrics were simply absent from the page. A row
that disappears is worse than one that fails visibly: a stated failure invites a retry, while a missing
row reads as a feature that was never built. `PortfolioSummaryCards` (now `PortfolioHero`) had the
correct branch all along. The `null` return survives for the genuine no-data-yet case — an empty
portfolio is not a failure and must not claim to be one — so the two states are now distinct props
rather than one absent value.

**And `e2e/errors.mjs` shows how it hid.** Its count of panels reporting the failure went **8 → 10**
when these were fixed: the two surfaces it existed to cover had never been in its own tally, under an
assertion (`hits >= 4`) loose enough not to notice. A floor that far below the real count cannot fail,
which is the same "passes vacuously" shape as the Sharpe clamp test. It was `>= 10` until the
product pass merged five summary cards and the chart card into one hero and dropped the
contributions strip; it is `>= 8` since 2026-10-17 — fewer surfaces, each still reporting, and the
hero says *the server didn't respond* in the same words as the panels under it.

### Risk metrics (`portfolioKpis.ts`)

Sharpe, Calmar and top-5 weight predate the rest. Volatility, Sortino, beta/correlation, drawdown
detail and Herfindahl effective holdings were added because the tab reported return *per unit of
risk* without ever reporting the risk, and because a top-5 weight cannot tell five equal positions
from one dominant one.

- **Beta is measured only over days when NEITHER series saw a flow.** The benchmark is a flow-matched
  hypothetical carrying the portfolio's own cost-basis line but no `external_flow_eur`, so netting a
  flow out of it means inferring one from the cost delta — the same asymmetry that fabricates a loss
  on every sale date in `externalFlow`'s fallback. That would bias beta on exactly the days the
  portfolio traded. Dropping the pair costs a few days a month and biases nothing. `sampleDays` rides
  along so a thin window declares itself instead of showing a confident slope.
- **Volatility, Sortino *and Sharpe* are `null`, not `0`**, below the minimum sample or with nothing to
  divide by. A `0` meaning "unknown" being read later as a fact is the most repeated bug in this
  codebase.

  **Sharpe was the exception until 2026-08-05**, because it predates the risk row and was missed when
  the other two were written. It returned `0`, and that was reachable in one click: selecting **MTD** in
  the first days of a month leaves 2–3 daily returns, so the card drew a green `0.00` captioned
  *Risk-adjusted return* beside a dashed Volatility and Sortino. Sharpe is the worst possible place for
  this substitution — `0.00` is a *plausible* Sharpe, so unlike a 0% volatility nothing about the number
  invites doubt. Its negligible-volatility branch is `null` now too: return per unit of risk is
  undefined when there is no risk, and a flat series is what a stale feed looks like.

  Its clamp test had also been passing vacuously for the same reason — the old series' σ tripped the
  negligible-volatility early return, so it asserted `|0| <= 10`, true of anything. It now uses a
  high-return/low-σ series that reaches the clamp.

- **`concentrationPct` is `null` too, and it was the worst of the three**, because its zero does not
  merely fail to inform — it *reassures*. The tone ladder calls anything under 50% good news, so an
  unpriced portfolio drew a green `0.0%` asserting the five largest holdings are none of the book.
  `herfindahlConcentration` already returned `null` on that exact condition, and the two now share a
  card (the effective count moved into the Top-5 footnote when *Effective Holdings* was replaced), so
  the green zero appeared with the one qualifier that would have explained it silently dropped out.
  Both absences are the same condition, so the footnote says *No priced positions* rather than leaving
  a bare "Concentration risk" under a dash.

  **The same lens caught `_compute_rsi` on the backend, where the stand-in was a *maximum*, not a
  zero.** `avg_loss == 0` returned `100.0`, conflating "an unbroken advance" with "nothing moved at
  all" — RSI is undefined on a flat series, and 100 is the strongest overbought reading the scale
  has. `_compute_buy_score` then scored it **0 of 10** on technical timing, while its own `rsi is
  None` branch scores an unknown at a neutral **5**: the fabricated value was ten points worse than
  admitting the metric was unmeasurable, and the honest branch already existed. Reachable on a
  halted, delisted or fixed-NAV listing, which the watchlist is far more exposed to than the
  portfolio. Every earlier instance of this lens found a zero, so grepping for a suspicious `0`
  would not have found this one.

  **The lens worth reusing:** when a metric can be unknown, ask what its *stand-in value would claim*.
  A `0` volatility looks broken and gets noticed; a `0` Sharpe and a `0%` concentration both look like
  answers, and the concentration one looks like a *good* answer. Severity tracks plausibility, not
  magnitude.

  **The ladder's top rung is a stand-in that makes a claim in *prose*, and the drawdown pair was
  it** (fixed 2026-08-17). `maxDrawdownPct` returned `0` with no measurable returns and
  `RiskMetricsCards` renders a zero max as *"Never below its opening value"* — green, because the
  current drawdown is zero too. Both are `number | null` now, with `sampleDays` beside them; a
  measured zero keeps saying "never fell", because there the sentence is true. Same sweep:
  **`winRate` had counted an unvaluable holding as a losing one** (valued at 0.00, so its gain is
  `−cost`) and is now `null` with the excluded count named. Note where the zeros in this family come
  from — `0` for unknown, `100` for `_compute_rsi`, and now a *sentence* — so the question is what
  the stand-in asserts, never what value it happens to be.

  **A pre-inception day is measurable and zero, and every compounding walk seeds from the first
  valued point** (`firstValuedPoint`, 2026-09-12). The backend emits `market_value_eur: 0,
  cost_basis_eur: 0, unpriced_holdings: 0` for each weekday before the first lot — verified live,
  105 such points before this account's first purchase — so `isMeasurable` keeps them, and a walk
  seeded from `series[0]` stayed at zero through every return: `maxDrawdownPct` printed the same
  green "Never below its opening value" over a real fall by a second route the 08-17 fix did not
  cover, and Dashboard's header published `(+0.00%)` for two percentages whose denominator was that
  zero. `periodChange` (extracted from the Dashboard memo, so it could be tested) returns `null` for
  both, and reads the first and last *measurable* points like every other consumer of the series.
  Two details the fix needed: the seed is the first point with `market_value_eur > 0`, never
  `series[0]`; and **only the returns dated after the seed are walked** (`returnsAfter`) — the
  inception pair's Modified-Dietz return is the move from nothing held *to* the seed, which the
  seed's value already contains, and applying it again fabricated a −2% opening drawdown for a lot
  bought at 100 that closed at 99. The 2Y button crossed this account's inception on 2026-05-28,
  so it is latent here and live on any younger account or after a backfill.
- **The hero's period return divides by the capital that earned the gain** (2026-10-17):
  `periodGainPercent` is the range's gain over its Modified Dietz average invested capital
  (`modifiedDietzCapital`, the denominator each heatmap month already used), walked from the first
  valued point of the measurable span. It divided by the *opening cost basis* before, which
  overstated every range money was paid into — a year of monthly deposits earns on all of them,
  and only what was held at the start was in the denominator — and mis-sized a book carrying old
  gains, which earn on their value, not their cost. Same sign as the gain by construction; on 1Y
  it sits near the XIRR card. `null` when that capital is not positive, never a stand-in.
- **Sortino's two nulls are told apart** (`sortinoDetail`, 2026-10-14). Too few returns and no
  downside deviation both returned `null`, and the card captioned every null *"No down days"* — a
  claim about the range, made over a window too short to have measured it. Only `no_downside` may
  say it; too little history reads *"Not enough history in this range"*, like Volatility.
- **`dailyReturnSeries` exists because `dailyReturns` drops days with nothing to divide by**, so the
  nth return is not the nth calendar point. Indexing the input by return position to name a
  drawdown's peak picks the wrong day.
- **The *current* drawdown leads and the worst one is the footnote.** Showing only the max reads as a
  live warning long after the recovery.
- **`winRate` judges only what could be valued**, and reports how many it left out. It excludes on
  the two-clause predicate `market_price === null || market_value_eur <= 0`, shared in spirit with
  `rebalance.ts` — a missing FX rate leaves the price populated and zeroes the value, so the
  one-clause form reads the holding as priced and gives it a real-looking loss.

### Target allocation and drift (`rebalance.ts`)

Targets live in **localStorage**, following `ForecastTab`'s precedent. Deliberately not a table plus
an endpoint: `/api/` is proxied publicly and every write is auth-gated, so a route that stores
portfolio intent is a larger surface than this earns. The cost — targets do not follow you to another
browser — is stated in the panel. `readTargets()` drops a stored value that is not a usable percent
rather than coercing it, because one NaN propagates into every drift on the page, and survives
corrupt JSON rather than taking the tab down.

Four rules, each a wrong number the other way:

- **A missing target means unmanaged, never 0%.** Reading absence as zero advises liquidating every
  holding whose target has not been set — all of them on first use. Clearing the input therefore
  *removes* the target; `0` means "hold none of this", and those are different instructions.
- **Targets are never renormalised to 100%.** The shortfall is reported instead. Scaling invents a
  target nobody chose, and the invented one moves whenever an *unrelated* target is edited.
- **An unpriced position has no weight rather than a zero weight** — and "unpriced" means **either** of
  the two ways the backend fails to value one. It values a holding with no cached price at 0.00, so naive
  drift advises buying its entire target when the position may be the largest one held: the SBI shape.

  **`market_price === null` alone does not catch it, which was a live gap until 2026-08-05.** When the
  price resolves but its **FX rate** does not, `get_positions_breakdown` sets `market_value_eur = 0.0`
  and leaves `market_price` populated — so the holding read as *priced*, took a 0% weight, and drift
  advised buying the whole target. Same bug, second route. A missing FX rate is not hypothetical:
  Frankfurter cannot serve TWD at all, which is the entire reason `WARM_CURRENCIES` exists.

  Both `rebalance.ts` and `currencyExposure.ts` therefore test `market_price === null ||
  market_value_eur <= 0`. The old narrow predicate was justified in a comment by "a *priced* holding
  genuinely worth zero is a real 0%, e.g. a fully-sold one" — **and that premise was false**:
  `get_positions_breakdown` selects `is_open == True`, so a sold-out security has no open lots and never
  reaches the client at all. Two tests asserted the wrong behaviour on the strength of it. What a zero
  value on a priced position actually means is the FX failure above, or a zero-quantity open lot, which
  is a data anomaly that should equally not drive advice.

  Note this is a *narrower* question than `summary.unpriced_holdings`, which is the backend's own count
  and the one to trust for the headline — the client predicate exists only to decide per-position
  weighting.
- **Targets key on `security_id`, not symbol**, because identity is `isin + exchange` and ASML is two
  securities.

Two further refusals. An empty plan is **not** `balanced` — vacuous truth renders *nothing to do* on
a portfolio nobody has configured. And `judgedCount` exists because counting only rows *outside* the
band cannot tell an all-clear from an empty comparison: zero judged rows must say so, which is also
wrong-with-a-healthy-backend when every target sits on an unpriced holding.

### Currency exposure (`currencyExposure.ts`)

`securities.currency` is the currency a listing **trades** in. For a direct holding that is also the
economic exposure; for a fund it need not be, and here often is not — a EUR-listed S&P 500 tracker is
quoted in EUR and carries USD risk. Folding it into the EUR bucket is confidently backwards on
exactly the positions that prompt the question.

**Nothing is re-attributed, and the ETF look-through table cannot fix it**: `app/etf_mappings.py` maps
*regions*, and regions do not determine currency — "Europe" spans EUR/GBP/CHF/SEK, "Asia Pacific"
spans JPY/AUD/HKD/TWD. Funds are counted where they trade, with their share of the book named on
screen and the reason given, so the rows cannot be mistaken for an FX position. The fund set comes
from the ETF bucket of the allocation response already on the page, since `Position` carries no asset
type; matching is by symbol, so a stock sharing a held fund's ticker would be flagged — accepted,
because the flag is a caveat rather than a figure.

Unpriced positions are excluded and counted, as above, and `foreignQuotedPct` returns `null` rather
than `0` when nothing is priced: no positions is an unknown exposure, not an unhedged-free one.

### The forecast (`forecast.ts`)

The Forecast tab's compound-growth projection, and the baseline it starts from. It was inline in
`ForecastTab.tsx` until 2026-09-08, written out **four times** — the horizon table, the scenario
cards, the sampled chart series and that series' hand-copied final point — and the tab had no tests
at all. Two rules, each a wrong number on screen first:

- **Money In starts at what was paid in, never at what the book is worth.** The chart's grey band
  was seeded with `total_market_value_eur` and then grew by the monthly contribution, so it asserted
  that every gain the portfolio had ever made was contributed and the gap to the green band showed
  only *future* gains. The baseline is `windows['all'].money_in_eur` from
  `/api/portfolio/contributions` — the app's one answer to "how much have I paid in", the figure the
  Money In per Month card and the value chart's Money In line both publish, already in Dashboard's query
  cache so it costs no request. Never `total_cost_basis_eur`, which is *deployed* and counts a
  rotation twice.
- **`Portfolio Value = Money In + Investment Gains`**, on every row and every point, on the rounded
  figures (`gains` is the rounded remainder — the `fund_residual_eur` rule). So "Investment Gains" is
  every gain including those already made, and the year-0 gap between the bands is today's unrealised
  profit. Before, the *table's* column of the same name was a different quantity from the chart
  band's — `monthlyContribution × months` with no seed at all — and the three columns did not sum.

Three consequences. **The seed is Total Value** (holdings + cash) whenever `cashIsTracked`, because
money in counts a deposit the moment it lands whether or not it is invested yet, so measuring it
against holdings alone understates today's gain by exactly the idle balance — and it makes the
Current button agree with the hero card. **"Start from 0" zeroes both sides**: it is the from-scratch
scenario, and carrying today's money in would report a negative gain of that size. **A baseline that
could not be loaded is `null`, never 0**: the grey band is omitted, Money In and Gains read `—`, and a
notice under the chart says so, while Portfolio Value — which never needed the baseline — is
unaffected. Falling back to 0 or to the start value would redraw the very lie the module removes.
**The entered rate is the effective annual rate**: it compounds monthly at `(1 + r)^(1/12) − 1`,
so twelve months at r grow the seed by exactly r (pinned in `forecast.test.ts`). It was `r/12`
until 2026-10-14, which turned 8% entered into 8.30% a year — a projection more generous than the
rate on screen. The end-of-period annuity is unchanged and conventional.

**The monthly contribution defaults to the owner's real pace** — the `12m` window's
`avg_money_in_per_month_eur`, rounded, floored at 0 — until a value is typed; the "My pace" button
returns to it. Only a typed value is stored (`forecast.chosenMonthlyContribution`). The old key
`forecast.monthlyContribution` was written back on every mount, so a stored `1000` was the default
rather than a choice: it is dropped, and any other legacy value is migrated as a choice. Without
contributions (failed load, or no `12m` window) the default stays 1,000.

Tests: `src/lib/portfolioKpis.test.ts`, `src/lib/rebalance.test.ts`,
`src/lib/currencyExposure.test.ts`, `src/lib/forecast.test.ts`, plus jsdom tests beside each
component and three checks in `e2e/errors.mjs`.

---

### Rolling risk and the drawdown ledger (`rollingRisk.ts`), the waterfall (`returnDecomposition.ts`)

Added 2026-09-13 for the Analytics tab; the rules are in
[performance-analytics.md](performance-analytics.md). The two things that belong here:
they reuse `portfolioKpis.ts`' primitives rather than re-deriving a return (`TRADING_DAYS`,
`RISK_FREE_RATE`, `FLOW_EPSILON`, `meanAndStdDev` and `returnsAfter` were exported for
exactly that), so the last rolling point equals the risk cards over the same window; and
every metric is `null` below its sample floor, never `0`. The Analytics tab never asks the
benchmark endpoint unless the Performance tab has a benchmark selected — a cache miss there
is a Yahoo request, and it is handed the selection as a prop rather than re-reading storage.

### Monthly returns (`monthlyReturns.ts`, `MonthlyReturnsHeatmap`)

**`∏(1 + month) = 1 + YTD` is an identity, pinned in `monthlyReturns.test.ts`** (2026-10-14).
Each month used to start at its own first point, so the move from one month's last close to
the next month's first belonged to no month — a dozen trading days a year — and the YTD
column, a separate Modified Dietz over the year, did not equal the months beside it (2026
chained to about 25% against a YTD of 32%). `monthlyReturnRows` now measures each month from
the **previous month's last point** (flows weighted as before), and YTD is the months linked
geometrically. A year-long Modified Dietz could not keep the identity: its flow weighting is
an approximation that drifts furthest over long windows with large contributions.

- A month-end that held nothing is not an anchor: the inception month starts at its own first
  point, as before (the move from nothing to the first lot is inception).
- Leading months with no return sit outside the chain and the YTD is `partial`; a month with
  data but an undefined return **inside** the chain makes the YTD `null`. A month with no
  point at all is not a hole — the next month is measured across it.
- The running month is tagged `MTD` (cell and collapsed summary).

The edge trim and range flag on each window are `computeModifiedDietzReturn`'s, described in
[flex-and-sync.md](flex-and-sync.md).

---

## Visual hierarchy and KPI panels

The dashboard canvas and surface colours live in `index.css`; semantic gain, loss and warning text
keeps using `green-*`, `red-*` and `yellow-*`, with the accessible emerald, rose and amber aliases
owned by `tailwind.config.js` — `text-gain` / `text-loss` / `text-warn` are the same steps in one
class (*Design foundation* below). New chart series take `lib/chartPalette.ts`.
The page shell is centred at `max-w-[1400px]` so the wide desktop layout remains readable.

`ui/KpiCard.tsx` owns both KPI renderings. A standalone `KpiCard` is used for the summary and hero
figures; `KpiPanel` contains `tile` cards for dense metric rows, with one panel border and shared
hairline dividers. A hero consumes two grid tracks, so any explicit desktop grid must count occupied
tracks rather than cards: five summary cards need six tracks and the Analytics hero plus five detail
cards need seven. Loading states use the same rendering and track count as the data they replace.
A panel whose tile count is odd on a two-column phone grid passes `fillLastRow`, which widens the
last tile across the row rather than leaving a blank cell beside it.

---

## The mobile layout — one description, two renderings

The app is built to work at **390x844**, and the rule that keeps it that way is that a table and its
phone equivalent come from **one** `Column[]`, not two hand-written trees.

`ui/DataTable.tsx` renders a real `<table>` inside `ScrollableTable` at `>=sm` and a card list below
it, from the same descriptors. Fifteen tables times two renderings would be thirty places a
column can be added to one and not the other — the dominant failure mode above, in its worst form:
a diverging calculation eventually produces a number someone notices, whereas a column missing from
the phone produces *nothing at all*, on a device the author is not looking at. So `mobile` defaults
to `'detail'` (a new column reaches the phone unless someone explicitly hides it), `cell` takes the
view it is rendering into rather than being duplicated, and two test files hold the line:
`DataTable.test.tsx` drives both modes from one fixture, and `tableFamily.test.tsx` pins the
conventions across column sets plus a source scan for any raw `<table>` outside `ui/`.

**It never owns sort state.** The three sorting tables have genuinely bespoke comparators — nulls-last
over a string|number union, a rating consensus through a score table, a memoised portfolio total —
and pulling them in would need a `sortValue` per column, a fourth thing to keep in step with `cell`.
Rows arrive sorted. The contract every table already implemented and nothing stated: `onSort(active)`
flips direction, which is what makes the phone's single direction button correct.

Three tables deliberately stay tables, each argued in its own file: `CurrencyExposureCard` (the
comparison *between* rows is the point, and there is no identity column to promote), and the two
12-month x N-year matrices (`MonthlyReturnsHeatmap`, `DividendSummary`). They get a designed
`min-w-*` instead, because `ScrollableTable` applies none — which is why eighteen columns were being
*squeezed to min-content* rather than scrolled, and why its edge fades were describing a problem they
did not cause.

**Six things here were each a bug first:**

- **`justify-center` on a flex overflow container hides its leading items.** A centred row wider than
  its scroller overflows on *both* sides and `scrollLeft` cannot go negative, so the first tab sat
  ~150px off the left edge, permanently untappable. The tab strip is `justify-start` below `sm`.
- **A grid item defaults to `min-width: auto`** and so refuses to shrink below its content's
  min-content width. One wide table made a single-column track 392px inside a 358px page. The track
  yields, not the card: `[&>*]:min-w-0` on the paired grids.
- **A responsive base class loses to nothing at `>=640px`.** `p-4 sm:p-6` on `Card` would put
  `sm:p-6` inside a media query, where it beats a plain call-site `p-0` — and the sixteen KPI cards
  that then passed `text-sm` to `CardTitle` are two since `ui/KpiCard.tsx` was extracted, while
  fourteen sites override card padding. Hence `--card-padding` and
  `--card-title-size` as custom properties, in the bracket form (`p-[var(...)]`) so tailwind-merge
  still classifies them and a call site still wins.
- **`useMediaQuery`'s no-`matchMedia` fallback is desktop, and that is an invariant.** jsdom
  implements neither `matchMedia` nor `ResizeObserver` nor `scrollIntoView`. Falling back to desktop
  is what lets every pre-existing component test keep seeing the `<table>` it was written against,
  needs no vitest setup file, and keeps `e2e/a11y.mjs`'s `aria-sort` count non-zero. Flip it and two
  component tests plus an e2e check fail on day one.
- **An `absolute` descendant of a scroller escapes it unless the scroller is positioned.** The
  `sr-only` "Actions" header span resolved its containing block to `ScrollableTable`'s outer
  wrapper, sat at the wide table's far edge unclipped, and scrolled the whole Watchlist page 211px
  sideways at 1440. The scroller itself is `relative` now.
- **A card detail wraps, it never truncates its label.** Label and value share half a card; at 390
  "Withholding" read "Withh…" beside a long figure. The pair is `flex-wrap` with the value
  `ml-auto`, so it drops under its label instead.

**`MAX_RANGE_DAYS` is the other cross-language constant**, and it drifts with no other symptom:
`dateRanges.ts` clamps the ALL button to `365 * 5` precisely so it never asks
`/api/portfolio/value-over-time` for a span the router's own `max_days = 365 * 5` returns 400 for. The
clamp lands *on* the boundary — the client sends exactly `max_days` and the server compares with `>` —
so an off-by-one on either side breaks ALL for anyone with enough history while both suites stay
green. `tests/test_range_limit_agreement.py` reads both files and pins them **equal**, not merely
compatible: a client that shrank to one year would satisfy "not larger" and silently truncate.

`lib/breakpoints.ts` holds the two boundaries as numbers because a Recharts axis width is a prop and
a card list is a different DOM tree — neither is expressible as a `sm:` utility. Everything that
*can* stay in CSS does. `breakpoints.test.ts` pins those constants against Tailwind's own scale and
fails if `tailwind.config.js` ever gains a `screens` override, since one boundary written down in two
languages is the same failure mode again.

**`e2e/mobile.mjs` is the only thing that can see this class of bug** — horizontal overflow is a
property of the assembled page at a real width, and jsdom loads no CSS. Read its docblock before
"fixing" an overflow: `body { overflow-x: hidden }` clips rather than fixes, kills `position: sticky`,
and makes the check pass vacuously. Two assertions are paired specifically to catch that.

**Every value axis is `width="auto"`** (`chartAxisFamily.test.ts` scans every `<YAxis>`): a fixed
width is sized for one currency, and the base currency switches — "CHF 120k" lost its leading "C"
on axes sized for "€12k". Category axes of symbol names keep a fixed width, deliberately. **Date
axes go through `lib/dateAxis.ts`**: day labels up to ~100 days, otherwise one explicit tick per
month, so a month+year label cannot repeat.

Chart heights are CSS on a wrapper plus `height="100%"`, hoisted to a module constant per file — or
to the shared chart component when two files draw the same chart — so the chart and its
loading/error/empty states cannot drift. `DIVIDEND_CHART_BOX` is the second kind: the literal was
written out **four** times across `DividendsTab` and `DividendTtmChart` before they were given one
owner. `PerformanceAttribution` is the exception and keeps a numeric height: its height is
*data*-driven, one row per security, so a CSS height would squash thirty bars into 240px.

### Colour by identity, never by row position

Five places now assign a categorical colour, and all five assign it to the **entity**:
`sectorColors.ts` (a fixed taxonomy), `benchmarkColors.ts` (position in the full `/benchmarks`
list), `allocationSectorPaint` (canonical position, with a neutral ramp for the tail),
`dividendColors.ts` (position in the response's `stack_order`) and the crypto donut, which calls the
same module's `seriesColor(key, order)` with `/api/crypto/portfolio`'s `color_order` — valued coins
by CoinStats' market-cap rank, so a coin overtaking another by value cannot repaint it
([crypto.md](crypto.md)). `dividendColor` is a thin wrapper over `seriesColor`. Every one of them was a positional
palette first, and each was found the same way — something that should have kept its colour did
not: deselecting a benchmark recoloured the other two, a sync that reordered the chart moved
`Unknown`, and changing the Dividends range repainted most of the stack.

The value chart's own lines are a sixth, and the simplest: `PORTFOLIO_SERIES_COLORS` in
`benchmarkColors.ts` gives Value, Money In, Cash and Profit/Loss one fixed hue each (2026-10-17).
Before, the benchmark palette's first slot was the portfolio line's own blue and its sixth the
Cash line's teal — comparing against the S&P 500 drew it in the portfolio's colour — and Cash and
Profit/Loss sat ΔE 5 apart for full colour vision. The benchmark palette was then re-picked by a
greedy max-separation search against those four, so the three a reader can select at once clear
ΔE 15 from each other and from every portfolio line; `benchmarkColors.test.ts` measures both
sets, and a re-step that brings any pair closer fails there.

They are deliberately **not** one helper. What they share is the rule, not the code: a fixed
taxonomy, a server-supplied list, a canonical position plus a ramp, and a server-supplied rank plus
a ramp have different tails, and one signature covering all four would carry every tail as an
option. The shared thing is written here instead, and the family question is asked per module.

`--viz-series-1..8`, `--viz-series-muted-1..5` and `--viz-series-other` in `index.css` are the
dividend stack's palette, and the crypto donut's; slots 1, 4, 5 and 6 **are** `--viz-sector-1..4`, which is why re-stepping
it left the Allocation and look-through tabs alone. The measurements behind the hexes are asserted
by `lib/dividendColors.test.ts`, which reads them back out of `index.css` — including each card
surface, so a theme re-step that makes a muted step invisible fails there. Its ramp is five steps
and `sectorColors.ts`'s is seven; one set sized for both would be spaced for neither, so they stay
separate.

---

**The Dividends tab's two views come out of one `DividendStackChart`**, for the same reason a table
and its card list come from one `Column[]`. The monthly bars and the rolling twelve-month bars are
the same picture over different buckets — same symbols, same colours, same solid/dashed split — and
drawn twice they would have been two fifty-line recharts trees plus two copies of the tooltip's
payload plumbing, which is two places a series can be added to one and not the other. The axis
formatter and the tick-density expressions live there too, so the two views cannot drift on those
either; the axis formatter was already duplicated verbatim between the two files.

**Nothing in the component suite can see any of it.** `DividendsTab.test.tsx` mocks recharts'
`ResponsiveContainer` to an empty div, so no `<Bar>`, `<Line>` or `<Tooltip>` ever mounts — the
stacked rebuild is invisible there, and was to the chart it replaced. Coverage of the transform
belongs in `src/lib/dividendChart.test.ts` (pure node, no jsdom); the component file covers
controls, captions, the legend and the empty/error states, which do render. Do not "fix" the mock:
making it render children needs a fixed-size `<BarChart width height>` and buys flaky zero-width
charts.

---

---

## Two books — the Stocks | Crypto mode

The header's **Stocks | Crypto** switch (`ModeToggle.tsx`, immediately before `ThemeToggle`) swaps
the whole page under the header between the stock book and the crypto book
([crypto.md](crypto.md)). A **mode, not a tab**: the crypto view shares no figure with the stock
view, and a tab beside Performance would sit in the same strip and invite reading one number
against the other. `lib/portfolioMode.tsx` holds it, persisted per browser through
`readStored/writeStored` under `ibkr-portfolio-mode` and **validated on read** — anything but
`crypto` is stocks.

- **`AppHeader.tsx` / `AppFooter.tsx`** are `Dashboard`'s header and footer moved verbatim
  (2026-09-28). The shared controls live there — title, base currency, the mode switch, theme, admin
  key — and each view fills three slots with its own parts (`status`: last/next sync and warnings;
  `actions`: its Sync button; `below`: the result of a press), keeping the queries and mutation that
  feed them. The header knows nothing about IBKR or CoinStats.
- **`CryptoDashboard.tsx`** is eager and small; the page itself, `CryptoOverview.tsx`, is lazy
  behind `LazyTabPanel` (its own chunk, and its own boundary so a crash leaves the header — and the
  way back to stocks — working). `App.tsx` wraps the whole crypto view in a `CryptoBoundary` whose
  fallback offers **Back to stocks**: the mode is persisted, so the app-wide boundary's Reload would
  land straight back in a crashing crypto view.
- **Refusals are states, not errors.** `/api/crypto/*` is private and fails closed, so
  `lib/cryptoAccess.ts` tells **locked** (401: this browser has not presented the key — the lock
  button fixes it) from **no key on the server** (403, `ForbiddenError` in `api.ts`, or
  `health.write_auth_enabled === false`: no browser can fix it). Crypto reads never retry a
  refusal and status polling stops at one.
- **No tab strip in crypto mode.** The e2e suite holds the page to exactly one `tablist`, and the
  mode switch is a `role="group"`, never tab roles.

### `SegmentedControl` — the one toggle group

`components/ui/SegmentedControl.tsx` is every `role="group"` + `aria-pressed` button row in the
app: the mode switch, the crypto chart's Value | P&L and ranges, and the six groups Activity (×2),
Analytics (×2), Dividends and Look-through used to hand-roll — one of which had already lost its
`aria-pressed`. Two looks (`segmented`, `buttons`), single-select or a set of independent toggles.
`SegmentedControl.test.tsx` scans `components/` so a seventh copy fails.

## The product pass (2026-10-16)

The owner asked for an end-user product in the getquin mould: polished, not complex, no stray
text. What that settled, so the next change keeps it:

- **One hero, not a wall of cards.** `PortfolioHero` is the top of the Performance tab: total value,
  the range's *gain* (value change less net money in — a value change that counts deposits reads
  as growth that was never earned), the range picker, the area chart, and a five-figure strip
  (cost basis, unrealized, realized, cash, positions). The two KPI panels sit under it with
  sentence-case labels, no icons, and colour only on returns and drawdowns — a green Sortino is
  decoration.
- **Names go through `lib/securityName.displayName`** wherever a person reads them. IBKR's
  descriptions are upper-case, truncated and carry listing noise; GLEIF's legal names are
  upper-case too. Display only: sorting by name sorts the display name, and every lookup, export
  and `title=` keeps the raw string. A name that already has lower-case letters keeps its casing.
  Extend `KEEP_UPPER` / `EXPAND` there rather than special-casing a table.
- **`TickerAvatar`** shows the holding's logo mark when one is stored, else a monogram on a tint
  hashed from the ticker. Logos since 2026-10-17 at the owner's request, reversing "never a
  fetched logo": logo.dev learns the held ISINs (trade-off accepted), but the server fetches and
  stores them, so the browser loads them from `/api/logos` and the CSP stays `img-src 'self'`.
  Pass `securityId` to get the logo (Positions, Dividends and Activity do; a cash-flow row has none). No
  attribution or source text on screen. Fetching, the manual import and the routes:
  [logos.md](logos.md).
- **No definitions on screen**, which removed the `DataTable` glossary popover; a column `hint` is
  a header `title=` of a few words. No provenance either: the positions table's sibling/statement
  NAV badges, the dividends table's Source column and the tax report's feed badge are gone; the
  `est.` mark is the one survivor. Server `warnings[]` paragraphs (look-through fund coverage) stay
  in the API, not on the page.
- **Allocation is three donuts** with a value legend that is also the drill-down. The palette is
  unchanged: `--viz-sector-*` and the neutral ramp are pinned by colour-distance tests
  (`lib/dividendColors.test.ts`), and every softer green tried failed them against the orange slot.
- **Charts share one look**: no axis rules or tick marks (global CSS in `index.css`), horizontal
  grid only, a themed hover band, the value series as the one gradient area. Bar-chart labels use
  `interval={0}` so a long list does not silently drop every other name.
- **The forecast trend's chart is resizable** (2026-10-17, the owner: at 120 px the growth was
  barely visible). It opens at 260 px and a grip under it drags it between 120 and 560;
  `lib/useResizableHeight.ts` uses pointer capture (touch drags it too), the arrow keys move the
  focused grip, and the height is stored per viewer through `lib/storage.ts` when a drag ends.
  Reuse the hook for the next chart that wants it rather than a second drag handler.
- **Phone cards drop absent details** (`DataTable`'s `isAbsent`) and skip a disclosure that would
  reveal one line; a horizontally scrolling `SegmentedControl` keeps its selected option centred.
- **Font**: Inter, bundled via `@fontsource-variable/inter` and served from our origin, which is
  what the CSP's `font-src 'self'` allows.

## The fintech pass (2026-10-17)

The owner asked for a clean, slick interface that other people could be handed, keeping every
insight. What that settled, on top of the product pass:

- **Structure comes from surfaces, not lines.** A grey canvas, white cards one step up with a
  hairline border and a soft `--shadow-card` (none in dark mode, where elevation is the lighter
  surface),
  and one white app bar: `AppHeader` with the section strip under it in the stock view, closing
  itself with `bordered` in the crypto view, which has no strip. Ink is one step darker and
  hairlines one step lighter than before; every `hsl(var(--x))` reference kept working.
- **The headline figure is set like a balance** (`HeroAmount` in `PortfolioHero.tsx`): whole
  units at full weight, currency and cents a step quieter, one `sr-only` string for a screen
  reader. The range's gain is a pill in its own colour. An account that has never synced gets one
  first-run card ("No portfolio data yet" — `e2e/errors.mjs` asserts the phrase never shows during
  an outage, so keep the outage and the empty book apart) instead of a hero of zeros.
- **Every metric row is one hairline panel** — `KpiPanel` on Performance, Look-through and Crypto,
  its look where a row has its own layout (the Dividends tiles, the hero's strip) — so a figure
  looks the same on every tab: label, value, an optional chip, nothing else.
- **Tables are quiet.** `DataTable` has three densities, hairline row rules, a hover wash, and a
  footer placed by `footerLayout`: each total sits under the columns its `spans` name, whatever
  order the footer array lists it in, and spans that are not adjacent or that overlap throw
  rather than draw a total under a neighbour's header ([failure-modes.md](failure-modes.md)).
- **One chart look, enforced.** Every `<CartesianGrid>` spreads `CHART_GRID_PROPS` (solid
  hairlines along the value axis) and every money axis label goes through `formatAxisMoney`
  ("CHF 150k", "€1.2M", one decimal at most); `components/chartAxisFamily.test.ts` refuses a hand
  rolled copy of either. The value series is the one gradient area, and the crypto chart now draws
  its value the same way. Portfolio lines and benchmarks are coloured by identity (*Colour by
  identity* above).
- **A collapsible section remembers being opened** on this browser (`useRememberedOpen` in
  `lib/storage.ts`, keys under `section-open:`). A per-viewer convenience, so `localStorage` is the
  right home; anything but a stored `1`/`0` is the section's own default. A component test that
  renders a collapsible card clears `localStorage` in `beforeEach`.
- **Fundamentals colour only the two ends** of each metric's range, like the watchlist; the
  middle band is plain ink. A third, yellow band put a colour on nearly every cell.
- **Fundamentals list what is held.** The table, both earnings lists and `/api/fundamentals/status`
  read open lots only (`security_repository.held`); a sold position was a row of dashes counted as
  "no data yet". The sync fetches held securities only too, which only removes Yahoo requests
  (a sold security comes back with the next sync after it is bought again).
- **Copy stays figure-first.** A conditional caveat is one muted line (the currency-exposure fund
  caveat), never a paragraph, and the empty and error states say what is missing in a sentence.
- **A component module exports components only** (ESLint `react-refresh/only-export-components`,
  green since 2026-10-17). Providers sit apart from their hooks (`CurrencyProvider.tsx` beside
  `CurrencyContext.tsx`, `ModeProvider.tsx` beside `portfolioMode.tsx`, `useTheme.ts` beside
  `ThemeProvider.tsx`) and storage readers live in `lib/` (`benchmarkSelection.ts`,
  `rebalanceTargets.ts`, `lookthroughTone.ts`). The two table descriptors, `positionColumns` and
  `lookthroughColumns`, are the named exception in `eslint.config.js`: each closes over helpers its
  component renders with, and the table family test imports the exact array the component
  renders; editing either file reloads the page instead of hot-swapping it.
- **A chosen base currency the server cannot use is named, not hidden.** With no EUR→base rate
  stored, `/api/settings` answers `base_currency: EUR` and `base_currency_unavailable: CHF`
  (docs/pricing-and-fx.md); the header shows *No CHF rate yet — showing EUR.* and every figure is
  labelled EUR, because it is EUR.

## Design foundation (2026-10-17)

The owner asked for a fintech look with icons and colour, keeping every figure. This is the shared
layer the per-tab restyles build on. **Adopt it rather than re-deriving it**: a hex for a series, a
hand-rolled icon well or a second gain green is the duplication this repo keeps writing down.

### Tokens (`index.css`, wired in `tailwind.config.js`)

| token | Tailwind | use |
|---|---|---|
| `--background` / `--card` / `--raised` | `bg-background` / `bg-card` / `bg-raised` | page, card, one step above a card |
| `--border` / `--border-strong` | `border-border` / `border-border-strong` | hairline / emphasis |
| `--shadow-card` / `--shadow-raised` | `shadow-card` / `shadow-raised` | dark mode is an inset top highlight, not a drop shadow |
| `--gain` `--loss` `--warn` `--info` | `text-gain`, `bg-loss`, … (opacity modifiers work) | state ink |
| `--gain-soft` … `--info-soft` | `bg-gain-soft`, … | chip and icon-well backgrounds |
| `--chart-1..10`, `--chart-other` | `bg-chart-1` … `bg-chart-other` (hex: no `/alpha`) | categorical series |

**`text-gain` is `text-green-600 dark:text-green-400` in one class** — same colour, pinned equal
(with loss = red, warn = yellow) by `lib/chartPalette.test.ts` reading the config itself. The
older pair stays valid at its ~70 call sites; new code may use either. Every soft well keeps its
ink >= 4:1 (rose-600 is 4.7:1 on white, so 4.5 is unreachable under any visible wash).

### The chart palette (`lib/chartPalette.ts`)

Ten slots: blue, teal, orange, violet, lime, sky, rose, amber, pink, green. The opening is cool and
calm; rose and green — which a finance reader takes as down and up — sit at seven and ten.
Measured with the dataviz skill's model (OKLab ΔE×100, Machado-2009): every adjacent pair clears
CVD 8 / normal 15 in both themes, the first three clear it all-pairs, all slots sit in the OKLCH
band. Light lime, sky and amber are 2.2–2.4:1 on white, so a chart using them carries a legend
(all do). `chartPalette.test.ts` re-measures the values out of `index.css`; `lib/colorScience.ts`
is the shared measuring code (`benchmarkColors.test.ts` uses it too).

- `chartColor(i)` → `var(--chart-<i+1>)`; past slot ten, negative or non-integer → `CHART_OTHER`.
  **It never cycles**: an eleventh series in slot one's colour would claim to be the first.
- `chartColorFor(key, order)` — the key's position in a **stable identity order**, never its rank
  in the current view (*Colour by identity* above). A key not in the order → Other.
- `GAIN_COLOR`, `LOSS_COLOR`, `WARN_COLOR`, `INFO_COLOR`, `NEUTRAL_COLOR` (`hsl(var(--…))`) and
  `signColor(value, flatBand = 0)` for a mark coloured by direction; pass `FLAT_BAND_PCT` for a
  lumpy series so the bar agrees with its `DeltaChip`.

**Adopting it in an existing colour module.** The `var()` strings work as Recharts `fill`/`stroke`
and repaint on a theme flip with no JS. Not yet migrated (each area's own call): `--viz-sector-*`
and `--viz-series-*` (sectors, look-through, dividends, crypto donut — each pinned by
`dividendColors.test.ts`, so re-pointing them means updating its bars), `BENCHMARK_COLORS` and
`PORTFOLIO_SERIES_COLORS` (measured by `benchmarkColors.test.ts`), the Allocation donuts, and
AnalyticsTab's literal `GAIN`/`LOSS` hexes. When a module moves onto the palette, delete its hexes
and its measurement block rather than keeping both.

### Tone (`lib/tone.ts`)

`Tone = 'neutral' | 'positive' | 'negative' | 'warning' | 'muted'` (`KpiTone` is the same type).
`TONE_TEXT` (value ink), `TONE_CHIP` (well: `bg-*-soft text-*`; neutral is the brand blue, because
a grey well reads as disabled), `TONE_MARK` (one `text-*` token for an SVG drawn in
`currentColor`). Use these maps rather than writing the classes again.

### `KpiCard` (`components/ui/KpiCard.tsx`)

Every prop added here is optional; no call site moved.

| prop | type | notes |
|---|---|---|
| `label`, `value` | `ReactNode` | `value={null}` renders the dash, muted, ignoring `tone` |
| `tone` | `Tone` | value ink |
| `icon` | `LucideIcon \| ReactNode` | **a component** (`icon={Wallet}`) → tinted well before the label, card *and* tile. An **element** (`icon={<Wallet …/>}`, the old form) → drawn as before, top right of a card, not in a tile |
| `iconTone` | `Tone` | well tint; defaults to `tone` (muted when the value is absent) |
| `delta` | `{ pct, label?, projected?, flatBand?, title? }` | a `DeltaChip` pill beside the value. `flatBand` **defaults to 0** here; pass `FLAT_BAND_PCT` for dividends |
| `sparkline` | `(number \| null)[]` | oldest first; `null` breaks the line; < 2 points draws nothing. Stroke takes the series' own direction (last vs first) |
| `hero` | `boolean` | the emphasis level: larger figure, two grid tracks, taller sparkline |
| `tile` | `boolean` | inside a `KpiPanel` |
| `sub` / `footer` / `title` / `className` | | unchanged |

`Sparkline` (`ui/Sparkline.tsx`, geometry in `lib/sparkline.ts`) is usable alone: `values`,
`tone?` (omit for direction), `area?` (default on), `label?` (an accessible name; decorative
without one). Plain SVG, no Recharts — it may sit on the eager Performance tab.

`DeltaChip` gained `variant: 'text' | 'pill'`; the pill's wash and the text colour both read
`deltaTone(pct, band)` in `lib/delta.ts`, the one up/down/flat decision.

### `SectionHeader` and `CollapsibleCardHeader`

`SectionHeader` (`components/ui/SectionHeader.tsx`): `title`, `icon?: LucideIcon`,
`iconTone?: Tone`, `description?` (one short line — a figure or a state, never an explanation),
`aside?` (right-aligned figure or controls; wraps under the title on a phone),
`variant: 'card' | 'bare'` (`card` is a padded `CardHeader`; `bare` for a heading above a grid).
Use it in place of `<CardHeader><CardTitle>…` for every card title.

`CollapsibleCardHeader` takes the same `icon` / `iconTone`, plus `aside` — **text only**: it sits
inside the toggle button, and a control nested in a button is unreachable. `IconChip`
(`ui/IconChip.tsx`, `size: 'sm' | 'md'`) is the well both draw; it is `aria-hidden`, so the title
stays the name.

Icon choice: one lucide glyph per meaning, reused across tabs (the tab strip's below is the
vocabulary for whole sections). Icons label sections and headline KPIs; a dense row of six
metrics may well go without.

### The shell

- **The section strip** is icon + label pills (`TabsTrigger`'s `icon` prop): the active tab a
  primary-tinted pill, the rest muted. The `<button>` stays the 44px target `e2e/mobile.mjs`
  measures and the 36px pill is a span inside it; the focus ring draws on the pill. Arrow keys,
  roving tabindex and the one-`tablist` rule are unchanged. Icons: Performance `TrendingUp`,
  Dividends `HandCoins`, Look-through `Layers`, Allocation `ChartPie`, Activity `ArrowLeftRight`,
  Fundamentals `Gauge`, Watchlist `Eye`, Tax `Landmark`, Forecast `Telescope`.
- **The strip is sticky with a blur**, and lifts (a soft shadow) only once the page has scrolled
  (`lib/useScrolled.ts`). Padding sits on the tablist, not its sticky wrapper, so the tablist's own
  top is 0 while pinned — what `e2e/mobile.mjs` asserts. The header above it scrolls away; on a
  phone it wraps to two rows and pinning it too would eat a quarter of the screen.
- **Cards**: `rounded-2xl`, hairline border, `shadow-card` — in dark mode a 1px inset top
  highlight, which reads as a lit edge where a drop shadow would vanish.

### Performance tab (2026-10-17)

**Insights strip** (`PerformanceInsights.tsx`, logic in `lib/performanceInsights.ts`): four tiles
under the hero — top mover and biggest drag of the selected range (from the range's attribution
rows, never recomputed), largest holding by account weight, and the next dividend (the breakdown's
`upcoming`, skipping `pending` rows and anything before today). It adds no request: Dashboard
passes the attribution, positions, summary cash and `dividendBreakdown.upcoming` it already holds.
Each pick has two absences — `undefined` (not loaded) renders a dash, `null` (loaded, no such
row: nothing lost money this range) renders "None" — and an empty book renders no strip.
**`weightDenominator` is the one definition of "weight"**: valued holdings plus tracked cash.
`PositionsList` reads it too, so the tile's weight and the table row's cannot drift.
The KPI rows carry lucide components (`icon={Waves}`), grouped under bare `SectionHeader`s
"Returns" and "Risk & income"; Current drawdown's well is red only while in a drawdown, muted
otherwise (a falling arrow in a green well contradicts itself). Positions: `text-gain`/`text-loss`
gains, an avatar on the phone card's title line, and a slim weight bar under the figure on desktop,
scaled to the largest row (decorative — the figure is the value). The Monthly Returns cells are
washes of `--gain`/`--loss` (`bg-gain/10…/30` with gain ink, then `/80` and solid with
`text-background` ink, which is white on the light theme's deep green and near-black on the dark
theme's bright one). Attribution bars take `GAIN_COLOR`/`LOSS_COLOR`, Money In per Month
`chartColor(0)`. Not migrated, deliberately: `BENCHMARK_COLORS`/`PORTFOLIO_SERIES_COLORS` keep
their measured hexes — they were picked by a max-separation search against each other, which a
re-point onto the general palette would have to redo, and the section above still describes them.

### Dividends, Forecast, Tax (2026-10-17)

- **The dividend stack is on the palette.** `dividendColors.ts` assigns `chartColor(rank)` over
  `stack_order`; `seriesColor` *is* `chartColorFor`, so the crypto donut moved too. Ten identities
  instead of thirteen (the old five-step muted ramp is gone), Other is `--chart-other`.
  `dividendColors.test.ts` pins rank *n* → `var(--chart-n)` and the fold; the measuring is
  `chartPalette.test.ts`'s. Details in [dividends.md](dividends.md). This supersedes the
  `--viz-series-*` paragraph under *Colour by identity* above.
- **Growth is the Dividends headline.** The four KPIs are a `KpiPanel` with icons and delta pills
  (all at the dividend flat band, `FLAT_BAND_PCT`, so a pill agrees with every other chip on the
  tab). *Last 12 months* and *Next 12 months* draw sparklines from the forecast history
  (`forecastTrend(history, live)` — unwindowed like the tiles, ending on the live figure); a
  sparkline from the selected range would move under a figure that does not. The DividendsTab
  and the forecast card share the `['dividends', 'forecast-history']` query.
- **Inside the Dividends card**, blocks take `DividendSubheading` (small well, `text-sm`), one
  step below the card's `SectionHeader`. The card's controls sit in their own wrapping row, not in
  `SectionHeader`'s `aside`: the aside is `shrink-0`, and four controls wider than a phone pushed
  the page into horizontal scroll at 390.
- **Calendar rows** lead with a date chip and the holding's `TickerAvatar` (by `security_id`),
  the chart-colour swatch as a dot on the avatar. The per-position *Growth* column is toned at
  `RAISE_BAND_PCT`, the band the "raised · cut" count uses.
- **Forecast** has a horizon `KpiPanel` (value with sparkline, money in, gains — the chart's last
  point, so the tiles and the line end on one number); the chart reads `chartColor(0)` /
  `NEUTRAL_COLOR`; the scenarios are `KpiCard` tiles.
- **Tax** has a year summary `KpiPanel` — each tile a totals row from a table below, with that
  row's marks (`incl./excl. N est.`, `est.`, a dash for an unrecorded withholding or an unbuilt
  Steuerwert, never 0.00). Dividend rows show logos by ISIN; realized and holdings rows carry no
  ISIN or security id in `/api/tax/report`, so they show monograms.

### Activity, Fundamentals, Watchlist, header, Crypto (2026-10-17)

- **Activity's figures strip** (`lib/activitySummary.ts`): Events, Bought, Sold, Realized,
  Dividends, Net deposits for the filtered window, signed like the Amount column. **A figure is
  null unless every row of the window is in hand** — the endpoint pages, and a sum over page one of
  270 rows looks like the window's total. A window longer than a page is fetched once more at
  `SUMMARY_LIMIT` (500, the endpoint's `MAX_LIMIT`, the CSV's ceiling); past it the strip shows
  dashes and asks nothing. A filtered-out kind is a dash, not a zero. Net deposits reads the
  server's `counts_as_money_in` (a transfer is never money in). Dividends carry `est.` when an
  estimate is in the sum.
- **Activity type badges** are `TONE_CHIP` plus a glyph: Buy positive `Plus`, Sell negative
  `Minus`, Dividend positive `HandCoins` (est. muted), Deposit/Withdrawal neutral
  `ArrowDownToLine`/`ArrowUpFromLine`, Transfer warning `ArrowRightLeft`, corporate action muted
  `Split`. The word is the badge's name (`e2e/ledger.mjs` reads it); the glyph is `aria-hidden`.
  Dividends are `HandCoins` everywhere on the tab now, the Dividends tab's glyph.
- **Fundamentals**: logos by `security_id` in the metrics table, the ETF chips, the earnings
  calendar and the surprise history (20px there: one line, so the measured row height holds). The
  history header carries "N of M beat" (`lib/earningsBeats.ts`; a quarter with no verdict is in
  neither count). Beat/Met/Miss are `TONE_CHIP` positive/warning/negative.
  **The history page-size loop is fixed**: the left card is `self-start`, so its height is its
  content's and not the grid row's; stretched, it fed back until the history showed all ~170
  rows. `HISTORY_ROW_H` is 41 (measured), not 37.
- **Watchlist**: rows carry no security id or ISIN, so the avatar is the monogram — no backend
  change for it. `% 52w High` and `vs MA200` are pills on the column's existing verdict
  (`signalTone`: ±5 plain, below the high/MA is the good side); no `tabular-nums` on them, which
  under Inter gives the hyphen a figure's width. The header counts the list into the Score
  column's three bands (`scoreTone`).
- **Header**: two groups — currency + Stocks|Crypto, then theme, key, sync — at either edge of a
  full-width row on a phone, so the second row reads as one toolbar. The currency warnings sit on
  their own line under the row (beside the select they wrapped into a 12rem column and pushed the
  toolbar to a third row). The active book in `ModeToggle` takes the active tab's tint through
  `aria-pressed:` classes.
- **Sync outcomes** (stock and crypto) are one `ui/StatusBanner`: success/error/idle wash, a
  lucide glyph in place of `✓ ✗ ⏸ ⚠`, the message in plain ink. Idle is never green or red.
- **Crypto**: KPI tiles with icons, Today's change as the `delta` pill, a one-month sparkline on
  Total value from `/history` (gaps stay gaps); section headers on every card and notice. The
  allocation donut is on `lib/chartPalette.ts` (`chartColorFor` over `color_order`, ten slots, the
  rest Other) and no longer uses `dividendColors.seriesColor` — whose comment still names the
  crypto donut, and which the dividends now use alone.
