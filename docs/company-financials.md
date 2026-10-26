# Company financials — SEC EDGAR, `company_financials`, `/api/company-financials`

> **This file is authoritative for its subsystem.** Keep it accurate in the same change that
> moves the code.

The Company tab shows one company at a time: revenue, operating income, net income, diluted EPS,
free cash flow (before and after stock-based compensation, in total and per share), the diluted
share count, gross, operating, net and FCF margins, buybacks and dividends paid, cash and debt, and
dividends per share, quarterly, TTM or by fiscal year, over 5 years, 10 years or everything filed,
with a key-figures strip at the newest TTM point. **For every held US filer and the Nasdaq-100**
(owner's request, 2026-10-25), held or not. **Sourced from SEC EDGAR only** — never Yahoo,
never IBKR.

| piece | file |
|---|---|
| the two SEC endpoints, pacing, retries | `backend/app/services/edgar_client.py` |
| companyfacts → rows (every rule below) | `backend/app/services/company_financials_normaliser.py` |
| the pass: scope, CIKs, back-off, writes | `backend/app/services/company_financials_sync.py` |
| TTM, FCF, margins, CAGR, YoY | `backend/app/services/company_financials_service.py` |
| tables | `backend/app/models/company_financials.py` (migration `d1e3f5a7b9c2`) |
| reads | `backend/app/routers/company_financials.py`, `backend/app/schemas/company_financials.py` |
| first fill | `backend/app/cli/fetch_company_financials.py` |

## Sources

- `https://www.sec.gov/files/company_tickers.json` — ticker → CIK. Asked at most once a pass,
  and only when a held security or a constituent ticker has no answer on record (or the SEC
  listed none and `CIK_RECHECK_AFTER`, 30 days, has passed): `security_ciks` keeps a held
  security's answer, `sec_tickers` a constituent ticker's (GOOG's CIK is stored nowhere else).
- `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json` — every XBRL fact a company
  has filed, every period, every filing. One request per company per week.

**The SEC requires a User-Agent naming the client and a contact address** and answers 403
without one. It is `EDGAR_USER_AGENT` in `backend/.env` (`.env.example` has the placeholder
`"ibkr-portfolio-tracker you@example.com"`); never a literal in this public repository. It
travels only in the request header, is never logged, and `app/redact.py` masks it should it
surface in a message. **Without it the pass makes no request**: the scheduled run is recorded as
`skipped` with the warning *EDGAR_USER_AGENT is not set; company financials were not refreshed*,
and the CLI exits 1 (its `--dry-run` still lists what is due).

**companyfacts carries the standard taxonomies only** (us-gaap, dei, srt, …), never a company's
own extension concepts. A figure a company tags only under its own namespace is not in the
document at all, so no whitelist of extension names can reach it from this source (NVDA's capex,
below).

## Limits and bounds

- **5 requests a second** (`MIN_INTERVAL_S`, half the SEC's published 10), through the shared
  `paced_http.PacedGet`.
- **Bounded retries** (`RETRY_DELAYS_S`, 2 s then 10 s): a 429, a 5xx or a transport failure is
  retried twice, then that company is given up for the pass (`EdgarError`: no stamp, its stored
  rows kept, a warning on the run) and asked next week. **A 429 that survives the retries stops
  the pass**, as does a 401/403 (`EdgarRefused`): asking the next company would spend the same
  limit, or be refused the same way.
- **Staleness** (`STALE_AFTER`, 6 days): a company fetched within it is not asked again, so the
  weekly pass refetches everything once and a CLI run the same week spends nothing.
- **Misses back off** through `miss_backoff.miss_backoff_until` on
  `sec_companies.facts_checked_at` / `facts_miss_count`: a 404, or a document with no us-gaap
  10-Q/10-K figure this reads (an IFRS 20-F filer such as ASML or NU, a fund), is asked again on
  days 1, 2, 4, … up to every 30. A data answer clears both. `--force` asks anyway.
- **Refuse whole**: a company's rows are deleted and the new set inserted in one transaction
  (`CompanyFinancialsRepository.replace_rows`), committed per company.

## Scope

Two parts, one request per company (CIK) whichever brought it in.

**Held** — `CompanyFinancialsSync.in_scope`: securities with an open lot (`security_repository.held`),
listed on an exchange the price path quotes under the bare ticker (`MarketDataService.
EXCHANGE_SUFFIXES` entries with an empty suffix — NASDAQ, NYSE, ARCA, AMEX, BATS), and not a known
fund (`etf_mappings.is_known_etf_isin`). The symbol is spelled the SEC's way (`sec_ticker`: "BRK B"
and "BRK.B" are "BRK-B") and looked up in the ticker list. A Xetra or Euronext listing never
reaches EDGAR, a UCITS fund whose ticker collides with a US one (SMH) is excluded by its ISIN, and a
foreign private issuer listed in New York (ASML, NU) gets its CIK and then a miss: its document
holds IFRS 20-F figures only.

**The Nasdaq-100** — `CompanyFinancialsSync.universe`: the `UNIVERSE_SIZE` (100) heaviest
distinct companies in the stored basket of each fund in `UNIVERSE_FUNDS` (`QQQM`, resolved through
`EtfBasketRepository.fund_isins`, the same answer the logo pass's `--basket` reads). The evening
basket refresh keeps it current, so a reconstitution reaches the tab without a code change; the
weights are the index's modified market caps, so "top 100 by market cap" and "the Nasdaq-100" are
the same list (101 lines, GOOG and GOOGL one company; all 101 resolve to SEC tickers, checked
2026-10-25). Equity rows only, **whitelisted** (`INVESTED_ASSET_CLASSES`): QQQM's money-market
fund, futures and cash lines are no companies — and not `counts_as_invested`, whose fund marker
would admit the money-market fund. Foreign private issuers in the index (ASML, AZN, PDD, ARM, …)
get their CIK and then a miss, like a held one.

- **A constituent nobody holds gets a `sec_companies` row and never a `securities` row**: every
  Yahoo loop reads `securities`, and a hundred new rows there would be a hundred new price
  requests.
- **Each full pass stamps the companies it found** (`sec_companies.universe_seen_at`, one value
  per pass); `/companies` lists a company not held only while it carries the newest stamp, so one
  that left the index drops out of the picker, and a pass that found no basket stamps nothing (the
  last list stands, with a warning). A `--symbol` pass stamps nothing either. Stored rows of a
  former member stay; they are a few hundred kilobytes.
- **Size** (measured on the book's 18 companies, 2026-10-25): about 660 rows a company, so the
  Nasdaq-100 adds about 66,000 rows, 8–12 MB of SQLite with its index, and about 100 requests a
  week (under a minute at 5 a second). Each companyfacts document (1–5 MB) is read and dropped.

**One company, one set of rows** (keyed by CIK since 2026-10-25, migration `e5f7a9b1c3d6`).
Listings sharing a CIK (GOOG and GOOGL) are fetched once and stored once; `/companies` shows the
company under its held listing with the lowest symbol.

**Why mapping tables (`security_ciks`, `sec_tickers`) and not a column**: the answer "asked, and
the SEC lists no such ticker" needs a row of its own, with its own stamp (the `IsinIdentity` /
`SecurityLogo` pattern). The pass's bookkeeping (`fetched_at`, `facts_checked_at`,
`facts_miss_count`) is per company, on `sec_companies`.

## The normaliser

`company_financials_normaliser.normalise()` is the only code that reads companyfacts. Pure.

### Concept chains, per period

| metric | chain, in order |
|---|---|
| revenue | `RevenuesNetOfInterestExpense` → `Revenues` → `RevenueFromContractWithCustomerExcludingAssessedTax` → `…IncludingAssessedTax` (CRWD, ODFL) → `SalesRevenueNet` → `SalesRevenueServicesNet` (ODFL before 2018) |
| net income | `NetIncomeLoss` → `NetIncomeLossAvailableToCommonStockholdersBasic` → `ProfitLoss` |
| diluted EPS | `EarningsPerShareDiluted` → `EarningsPerShareBasicAndDiluted` (ALNY to 2021) → `IncomeLossFromContinuingOperationsPerDilutedShare` (ABNB; MNST after 2011), then net income / diluted shares (below) |
| operating cash flow | `NetCashProvidedByUsedInOperatingActivities` → `…ContinuingOperations` |
| capex | `PaymentsToAcquirePropertyPlantAndEquipment` → `PaymentsToAcquireProductiveAssets` → `PaymentsForCapitalImprovements` |
| SBC | `ShareBasedCompensation` → `AllocatedShareBasedCompensationExpense` |
| diluted shares | `WeightedAverageNumberOfDilutedSharesOutstanding` (then net income / EPS, below) |
| operating income | `OperatingIncomeLoss` |
| dividends per share | `CommonStockDividendsPerShareDeclared` → `CommonStockDividendsPerShareCashPaid` |
| cash | `CashAndCashEquivalentsAtCarryingValue` → `CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents` |
| cash and short-term investments | `CashCashEquivalentsAndShortTermInvestments` (MSFT, GOOGL, ADBE) |
| short-term investments | `ShortTermInvestments` → `MarketableSecuritiesCurrent` → `AvailableForSaleSecuritiesDebtSecuritiesCurrent` → `AvailableForSaleSecuritiesCurrent` |
| gross profit | `GrossProfit` |
| cost of revenue | `CostOfRevenue` → `CostOfGoodsAndServicesSold` → `CostOfGoodsSold` |
| buybacks | `PaymentsForRepurchaseOfCommonStock` |
| dividends paid | `PaymentsOfDividendsCommonStock` → `PaymentsOfDividends` → `PaymentsOfOrdinaryDividends` |
| debt, total | `LongTermDebt` → `LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities` |
| debt, noncurrent | `LongTermDebtNoncurrent` → `LongTermDebtAndCapitalLeaseObligations` |
| debt, current | `LongTermDebtCurrent` → `DebtCurrent` |
| shares outstanding | `CommonStockSharesOutstanding` → `dei:EntityCommonStockSharesOutstanding` (the cover's, dated weeks after the period: keyed to the newest period end its own filing reports). A count tagged per class is left out by companyfacts |
| equity | `StockholdersEquity` → `…IncludingPortionAttributableToNoncontrollingInterest` (the parent's first, as net income is) |

For **every period** the first concept that reports it wins, so a company that moved from
`SalesRevenueNet` to `RevenueFromContractWithCustomer…` in 2018 keeps one series. Quarters are
derived inside one concept before the chains merge (one exception, Q4 below).

- **`RevenuesNetOfInterestExpense` comes first** because only a bank or a broker tags it, and
  then it is the top line. IBKR stopped tagging `Revenues` after 2019; with it last, the chain
  fell to `RevenueFromContractWithCustomer…`, which for IBKR is commissions alone — a third of
  net revenues. An audit of the book's 16 SEC CIKs (2026-10-25) found no company that tags it
  and wants `Revenues`.
- **Net income is the parent's share before the consolidated one.** IBKR stopped tagging
  `NetIncomeLoss` after 2014; its `ProfitLoss` includes the non-controlling interests (about five
  times what its EPS is computed on). `NetIncomeLossAvailableToCommonStockholdersBasic` keeps net
  income, net margin and EPS on one base.
- **Gross margin** is `GrossProfit`, else revenue less cost of revenue (META, GOOGL and SPGI tag
  only the cost). `CostOfServices` is not in the chain: alone it is part of the cost (SPGI, ORCL).
  A company tagging neither (MA, IBKR) has no gross margin, never 100%.
- **Debt** (an audit of 22 companyfacts documents, 2026-10-25) is the tagged total, else the
  noncurrent and current halves added when both are tagged (MA, GOOGL's quarters), else absent.
  **`DebtInstrumentCarryingAmount` is not used**: ORCL's 10-Qs tag it per new bond issue (6.25B,
  14B, 18B), not as a total. **A tagged total of exactly zero is a slip** and falls through to the
  halves (ORCL FY2022 and AMD FY2021 tag 0 beside billions of notes). Commercial paper and
  short-term borrowings are not counted.
- **Net cash counts the short-term investments, or is unknown** (`_with_net_cash`). Cash alone
  showed NVDA, the book's most cash-rich company, as a net debtor (2026-10-25), so net cash was
  dropped, then rebuilt on the investments (2026-10-26): the combined line when tagged, else cash
  plus the investments chain. A period without them, of a company that tagged them within
  `INVESTMENTS_LOOKBACK_DAYS` (two years), has no net cash: NVDA's `MarketableSecuritiesCurrent`
  stops in late 2025, so its net cash ends there rather than turning into cash alone. A company
  that never tagged any (AVGO) holds cash alone. No debt tagged is no net cash, never debt of 0
  (a debt-free MNST has none). Enterprise value = market cap less net cash; EV/EBIT over TTM
  operating income, both in the key figures only.
- **EBITDA is not served**: depreciation and amortization are tagged inconsistently (GOOGL and
  AVGO tag depreciation alone, without the amortization of intangibles that dominates AVGO's).

The audit (one throwaway pass over the book's CIKs, nothing stored) flagged every place the
winning concept changes between adjacent periods with a jump over 30%; each was a real move
(AMZN's 2017 capex, NVDA's Q4 FY2019, AVGO's VMware year, IBKR's SNB quarter, ORCL's seasonality),
after the ORCL and ONDS fixes below.

### Durations, not `fp`/`fy`

`fy` is the fiscal year of the *filing*, and every filing repeats earlier periods, so facts are
keyed by (start, end) and classified by length: a quarter (75–105 days), a year (350–380 days,
from a 10-K), or a year-to-date rung in between. Forms: 10-K, 10-Q, their amendments and
transition reports; an 8-K's or a proxy's stray fact does not count.

- **The latest filing of a period wins** — that is how a restatement replaces the original.
- **Year-to-date ladders.** 10-Q cash-flow items run from the fiscal year's start. Two rungs of
  one ladder a quarter apart give the quarter between them, and the 10-K's year less the
  nine-month rung gives Q4. A reported discrete quarter beats a derived one. Flows and per-share
  figures are differenced; a weighted-average share count by day weight
  (`(Y·d_Y − N·d_N) / (d_Y − d_N)`, what a weighted average is). **A derived Q4 EPS is the year's
  EPS less the nine months'**, the usual approximation, exact only while the share count holds
  still; a derived Q4 share count is approximate too (each quarter's dilution is computed on its
  own). **A derived quarter's count more than 1.2× from every neighbouring quarter's that is not derived
  stays absent** (and may be refilled from net income / EPS): the year's diluted average can
  hold dilution its loss quarters leave out, and DXCM's 2020 Q4 came to 807M between 298M and
  418M (518M after the basic-count witness), REGN's 2013 Q4 to 13M. The neighbours include
  counts filled from net income / EPS, which run first: TTWO's 2013 Q4 (86.2M) is real beside
  its next quarter's 87.2M, its only reported neighbour a convertible-diluted holiday Q3.
- **Q4 across the chain, last resort.** When no single concept has both the year and the three
  quarters, Q4 = FY − (Q1 + Q2 + Q3) of the merged series, recorded with source
  `<concept> less Q1-Q3`. ORCL tags its years as `Revenues` and its quarters as
  `RevenueFromContract…`. Never for the share count.
- **Tagging slips that stay absent rather than charted.** A quarter context carrying the year's
  own figure (ORCL's 10-Ks tag the year's revenue on a March–May context) is dropped, and the
  ladder's Q4 replaces it. For a figure that cannot be negative (revenue, capex, SBC, shares,
  dividends), a negative derived quarter is dropped, and so is a year smaller than its own nine
  months (NVDA's FY2016 10-K tagged Q4's 0.115 dividend as the year's; the year then comes from
  the next concept in the chain). **A negative reported fact of such a figure is dropped as it is
  read**, before any ladder differences it (HON's Q1 2009 10-Q tags dividends paid as −224M; seen
  in the Nasdaq-100 fill, 2026-10-25). A year-to-date rung or year below two-thirds of the reported
  quarters inside it carries one quarter's figure, not a sum, and is dropped with everything it
  would derive: IBKR tags its nine-month and 10-K dividend contexts with the quarterly rate,
  which made every Q4 a 0.00 dividend (seen on the live tab, 2026-10-25). Its Q4s and fiscal
  years, and so every TTM, stay absent. A restated quarter moves the sum by less and never
  trips it (NXPI's 2023 10-Q re-tags Q3 2021's dividend at the 2023 rate). **A derived dividend
  of exactly zero stays absent too**: before 2020 IBKR filed no discrete quarter and tagged
  0.10 on every rung, which cannot be told from a quarter without a dividend, and a 0.00 must
  not stand in for the unknown. Its pre-2020 fiscal years still read one quarter's rate — the
  facts carry nothing that tells them from an annual payer's (accepted, documented here).
- **One end date per period.** Ends within a week are one period (NVDA tagged one 2010 rung as
  ending a day before its quarter).

### Splits: derived from EDGAR itself

The decision, against IBKR's `corporate_actions`: the Flex window covers only the time the
account held the stock and only what the statement period reached, so most of a company's split
history (NVDA's 4:1 in 2021 predates the holding) is not in the table at all. EDGAR always
carries it, because **every filing repeats earlier periods**, so the same period appears in
filings made before and after a split.

`adjustments()` walks the filings newest first. Each filing's per-share and share-count values are
compared with the already-adjusted values of newer filings for the same periods, and the ratios
vote for that filing's split factor, snapped to a plausible split ratio (`SPLIT_RATIOS`: 1:50 to
50:1 and the usual 3:2-style fractions; a per-share pair votes only when cent rounding cannot blur
it). The newest filing is the basis. Money is never split-adjusted.

- **The basic share count is the split's witness** (`SPLIT_WITNESS`, read for voting, never
  stored). A later filing that restates earlier periods' *diluted* counts (ASU 2020-06's
  if-converted method for convertibles) moves shares and EPS by reciprocal factors exactly as a
  split does; the basic count moves only in a split. When a filing's basic counts vote, they
  decide its factor. Before (found by the 102-company audit, 2026-10-25), REGN's counts before
  2014 read 25% high as a "5:4" and DXCM's 2020 quarters carried 3 for its 4:1.
- **A vote snaps the step from the newer filing, not the whole factor.** Two splits compound past
  the largest single ratio: NFLX's 7:1 (2015) and 10:1 (2025) make 70 for its older filings, which
  the whole-factor snap read as 1/14 with a thousands slip (71.4; found auditing all 102
  companies, 2026-10-25).
- **A filing sharing no period with a newer one** — every 10-K until the next 10-K — inherits the
  next newer filing's factor, **unless its share counts say a split fell between them**
  (`_neighbour_split`): each count is compared with the adjusted count of the nearest newer period
  ending within 100 days, and when at least two counts all step by the same common split ratio
  (2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 25, 30, 40, 50 or the inverse, within 5%), that is the
  filing's factor. BKNG's 25:1, CRWD's 4:1 and KLAC's 10:1 (2026) fell between a 10-K and the next
  10-Q: inheriting left every Q3, Q4 and fiscal year in the old basis while Q1 and Q2 were
  adjusted. ONDS's share issuance steps 1.50 and 2.08 — not unanimous, so inherited. Not every
  whole number: adjacent weighted counts differ by a few percent, and BKNG's 24.3 snapped to 24.

The same vote, per concept, undoes a **unit slip of a thousand or a million** (early XBRL filings
tagged some counts in thousands; PCAR's 2019 10-K its share counts in millions, STX's 2024 10-K
its dividends per share a million times over). A slip in a filing no newer one overlaps cannot be voted on, so a
**reported share count about 1000× off net income / EPS** for the same period is rescaled too
(ONDS tags some 10-Ks and Q1 10-Qs in thousands).

A filing slipped **in some contexts only** fits no one factor: DXCM's 2014 10-K re-tagged its 2012
quarters' EPS a million times over (−210000 beside three filings' −0.21) while its years stayed
right, and walking it voted the older filings a million times up to match. So before the walk
(`_drop_lone_slips`), a **per-share** fact a thousand or a million times off the value two other
filings agree on for that concept and period is dropped, and then so is that filing's same concept
the same factor off a single other filing, provided the filing's concept agrees with another
filing somewhere (one off everywhere is in other units, and the vote rescales it whole). Not share
counts: several filings shared thousands as a convention (MPWR's to 2013), so two agreeing is no
majority. Found when the EPS chain took `EarningsPerShareBasicAndDiluted`.

**Window**: a split after the newest filing is reflected once the next 10-Q is filed (it restates
the comparatives), so for up to a quarter the per-share history sits in the pre-split basis.

### Diluted shares when not reported

GOOGL tags `WeightedAverageNumberOfDilutedSharesOutstanding` only from 2022. For a period with no
count, it is **net income / diluted EPS** (source `NetIncomeLoss/EarningsPerShareDiluted`) when the
EPS is precise to 2% after its cent rounding — and **never from `ProfitLoss`**, which includes
non-controlling interests, nor from continuing operations' EPS, which net income is not over.

The other way round, a period with no EPS takes **net income / diluted shares** (source
`NetIncomeLoss/WeightedAverageNumberOfDilutedSharesOutstanding`): EXC's 10-Ks tag no year's EPS,
so it had no fourth quarter and no TTM EPS. Not from `ProfitLoss`, nor over a count that was
itself net income / EPS. A company paying preferred dividends reads them as common here.

### Fiscal periods, by period end

A quarter belongs to the fiscal year whose span contains it (from the year facts, or, for the
year in progress, from its year-to-date ladder's start), Q1–Q4 by its distance from that year's
start. A quarter starting within a week after a reported year's end opens the next year too:
until the six-month rung is filed, the new year's Q1 10-Q has no ladder, and the first fill
(2026-10-25) dropped SPGI's and NXPI's Q1 2026 for that reason. **The fiscal year is named after the calendar year it ends in** — NVDA's year ending
26 January 2025 is FY2025 — except a 52/53-week year ending in the first week of January, which
is named after the year before.

## Storage

`company_financials`: one row per (CIK, period end, fiscal period, metric) — Q4 and FY
share a period end, hence the fiscal period in the key — with `fiscal_year`, `value` (float),
`unit` (`USD`, `USD/shares` or `shares`), `source_concept`, `filed` (the newest filing behind the
value) and `fetched_at`. **A period never reported has no row; nothing is stored as 0.**

## Service and API

`GET /api/company-financials/companies` — companies with stored figures, one per CIK: the held
ones and the newest pass's Nasdaq-100 (above): `cik, security_id, symbol, name, isin, held,
currency, first_period_end, last_period_end, last_fetched_at`. A held company is named by its
listing with the lowest symbol (`security_id` set); one not held by its basket ticker, name and
ISIN (`security_id` null; the ISIN finds its logo).

`GET /api/company-financials/{cik}?period=quarterly|ttm|annual&range=5y|10y|max`
(defaults `ttm`, `10y`): the same identity fields, then `currency, period, range, as_of,
warnings, key_stats, series`, each series `{points: [{period_end, label, value, yoy}], cagr, yoy}` — every point
carries its own YoY by the series' rule (it may compare with a point before the range), for the
tab's hover — and `cagrs` `{y3, y5, y10}`. The series: revenue, revenue per share, operating
income, net income, diluted EPS, FCF, FCF after SBC, FCF per share, FCF after SBC per share,
diluted shares, four margins (gross, operating, net, FCF), dividends per share, buybacks, dividends
paid, cash, debt; every per-share figure is the figure over the same period's diluted shares.
`key_stats` is the newest TTM point's figures whatever `period` asked for (`label`, revenue with
its YoY and 5-year CAGR, net income, EPS with both, FCF and FCF per share, the margins, shares and
their YoY, DPS and the payout ratio, P/E, P/S, P/FCF, dividend yield, return on equity, buybacks,
dividends paid, market cap). A CIK the pass never looked at:
404.
A known one without figures: 200, empty points, a warning. Response models are pinned both ways by
`tests/test_company_financials_contract.py`. Public reads, rate-limited like every `/api/` read;
**no write route**.

Computed in `company_financials_service` only:

- **TTM** of a flow = the sum of four consecutive quarters (ends 80–120 days apart: COST's
  fourth quarter is 16 or 17 weeks, and at 105 it had no TTM at any year end; the normaliser's
  `QUARTER_DAYS` takes 75–120 for the same reason, or its year-end balances had no Q4); a missing
  quarter or a gap makes it null, never a partial sum. TTM shares = the mean of the four.
- **FCF** = OCF − capex; **FCF after SBC** = FCF − SBC; **FCF per share** = FCF / diluted shares
  of the same period. A missing input makes each null: IBKR reports no capex, so it has no FCF.
- **Margins** = the figure / revenue of the same period, as fractions (0.25 is 25%); null when
  revenue is missing or not positive.
- **Balances** (cash, debt) are the period end's; a TTM point carries its newest quarter's.
- **Payout ratio** = TTM dividends per share / TTM diluted EPS; null when the EPS is not positive.
- **P/E** = a close / TTM diluted EPS (a year's own EPS on the annual view); null when the EPS
  is not positive. **P/S** and **P/FCF** are the same close over revenue and FCF per share,
  **dividend yield** TTM dividends per share over it (`valuations`); every per-share figure is
  over the diluted shares, so all four share the EPS's split basis. No dividend is no yield,
  never 0%; a negative FCF is no P/FCF.
- **Return on equity** = TTM net income (a year's own on the annual view) / the mean of the
  equity at the period end and a year before it (within `YOY_SLACK_DAYS`); null when either is
  missing or not positive — a buyback-heavy company's negative equity makes no return.
- **Market cap** (key figures only) = the newest close x shares outstanding at the newest quarter
  end, when that count is within `MARKET_CAP_SHARE_TOLERANCE` (10%) of the newest diluted count
  (the quarter's own, else a quarter's or year's up to a quarter before: TTWO's two loss quarters
  have none, its year does); otherwise
  null, never the diluted count in its place. It declines TSLA (3.95B outstanding against 3.54B
  diluted) and LITE, whose two counts genuinely differ, rather than guess which is the cap. The `pe_ratio` series takes each period end's newest weekly close within
  `PRICE_MATCH_DAYS` before it; the key figures take the newest of the weekly closes and a held
  listing's daily close (`market_prices`), null when older than `PRICE_MAX_AGE_DAYS`. A close in
  another currency than the filings is not used. No growth is computed for it (`NO_GROWTH`). It
  is not the Fundamentals tab's `trailing_pe`, which is Yahoo's own figure over Yahoo's EPS; this
  one is over the EPS the tab shows beside it.
- **Split basis** (`eps_basis`): Yahoo split-adjusts closes to the day they were fetched, the
  normaliser restates figures to the newest filing. A split after that filing is in the closes
  and not yet in the EPS, so its ratio scales the closes up until a filing restates the figures.
  Only a ratio in `SPLIT_RATIOS` counts: Yahoo books a spin-off as a fractional split (SPGI's
  1.057 on 2026-07-01), which moves no share count. Its pre-spin-off closes stay as Yahoo
  lowered them, so a P/E before a spin-off reads that fraction low (accepted).
- **CAGR** over the range's first and last known values; null when the start is not positive,
  the end is negative, or they are under a year apart. **YoY** = the latest point against the
  point a year before it (±20 days, for 53-week years); null when that point is missing or not
  positive. **3/5/10-year CAGRs** (`cagrs`) compare the latest point with the one that many years
  before it (same slack) over the whole history, not the range; null also when the latest is
  negative (a root of a negative ratio is no rate). **All are null for the margins** (lead's
  decision, 2026-10-25): a growth rate of a ratio reads as a figure nobody means.
- **Annual** = the fiscal years as filed (the FY facts), not a sum of quarters.
- **Range** counts back from the series' latest period end, not from today, so a TTM at the
  range's start is computed from quarters before it.
- Labels: `Q3 FY2025` for a quarter or a TTM point, `FY2025` for a year.

### Why the reporting currency, not the base currency

Every figure stays in the currency the company reports in (USD for every filer this reads), and
is **not** projected into `app_settings.base_currency` like the rest of the app. Converting a
ten-year history at each period's rate would fold a decade of exchange-rate moves into every growth
rate and margin trend (a strong dollar would read as slower growth). Converting at today's rate
would change nothing a chart shows except the axis. `currency` on every response names it.

## Schedule

**Sunday 05:00 Berlin, weekly** (`COMPANY_FINANCIALS_REFRESH_HOUR`/`_DAYS` in
`scheduler_service.py`; job `company_financials_refresh`, upkeep group). 05:00 is the
fundamentals' hour, which runs Tuesday to Saturday only, and no portfolio, crypto or watchlist slot
is at 05:00 on a Sunday — so it collides with nothing, and since the hour is already in
`ALL_SYNC_HOURS` the deploy guard covers it with no ops change. Filings land on weekdays, so a
Sunday pass picks up the week's. Under its own gate, `EDGAR_GATE`, never `SYNC_PIPELINE`: EDGAR
shares no upstream with Yahoo or IBKR. It records a `company_financials_refresh` row in
`sync_runs`; a company EDGAR could not serve is a warning, never the status.

**First fill** (inside the container; the CLI's own `single_flight` fences its own process only):

    docker exec backend-portfolio-backend-1 python -m app.cli.fetch_company_financials --dry-run
    docker exec backend-portfolio-backend-1 python -m app.cli.fetch_company_financials

`--symbol NVDA` (repeatable; a held symbol or a constituent's ticker) narrows it, `--force`
ignores staleness and back-off. It records a `manual_company_financials_fetch` row.

## Prices

The P/E's closes come from Yahoo (`company_price_sync`, CLAUDE.md rule 1; enabled by the owner
2026-10-25): **one history request per company a week**, about a hundred, after the EDGAR pass.

- **Scope**: the companies the tab lists (`list_companies`), by CIK, asked by the SEC's ticker
  (Yahoo spells US listings the same, BRK-B). No `securities` row is read or written, so
  `yahoo_eligible` has nothing to filter and a constituent never joins the price loops.
- **Kept** (`company_prices`, `company_splits`, migration `f6a8b0c2d4e7`): the last trading day's
  close of each week, from `HISTORY_LEAD_DAYS` before the company's first stored period, in the
  quote's currency after `minor_unit_scale`; and every split Yahoo reports. Weekly rather than
  daily: a period end needs a close within a week, and daily would be five times the rows.
- **One basis.** A fetch reaches `OVERLAP_DAYS` behind the newest stored close. A split in that
  window not stored yet, an overlapping close more than `BASIS_TOLERANCE` off, or another
  currency means the stored history is on an older basis: the whole history is fetched again
  (a second request) and replaced as one set (`write_prices`).
- **Bounded**: a company at most once in `STALE_AFTER`; an empty answer backs off on the shared
  doubling schedule (`prices_checked_at`, `prices_miss_count`); a 429 or the process-wide hold
  abandons the pass with a warning (`yahoo_rate_limit`), and nothing is stamped.
- **Schedule**: Sunday 05:00 Berlin (`COMPANY_PRICES_REFRESH_HOUR`/`_DAYS`, job
  `company_prices_refresh`, upkeep group), under `SYNC_PIPELINE` like every Yahoo job; nothing
  else asks Yahoo on a Sunday. It records a `company_prices_refresh` row. By hand:

      docker exec backend-portfolio-backend-1 python -m app.cli.fetch_company_prices --dry-run
      docker exec backend-portfolio-backend-1 python -m app.cli.fetch_company_prices

  `--symbol`, `--force` as above; it records a `manual_company_prices_fetch` row.

## Known gaps and out of scope

- **NVDA capex, FY2013–FY2021**: NVDA tagged its annual capex ("purchases related to property
  and equipment and intangible assets") as an `nvda:` extension concept, which companyfacts does
  not carry. Its Q1–Q3 capex exists from the 10-Q ladders up to mid-2020, but every Q4 and every
  year in between is null. FY2023's capex survives only as the nine months and the year (later
  filings' comparatives), so only its Q4 is derived. NVDA's annual FCF has FY2010–FY2012 and
  FY2022 on, and its TTM FCF starts at Q3 FY2024 (checked on production, 2026-10-25). Reaching extension concepts would need each
  filing's XBRL instance, a different source.
- A unit slip in a monetary concept that no newer filing overlaps is not caught (the share-count
  check has NI / EPS to compare with; revenue has nothing).
- **KDP before 2018** mixes Dr Pepper Snapple's and Keurig's filings (the 2018 reverse merger moved
  the fiscal year): its 2016-2017 share counts jump. Accepted; the figures since 2018 are one company's.
- **A 10-Q missing from companyfacts**: ALNY's Q1 2026 is in no fact, so it has no TTM from
  then until its Q1 2027 10-Q restates the quarter. Deriving it as six months less Q2 is not done.
- **No EPS tagged at all**: BKR after 2024 (no EPS concept, no diluted count to derive it);
  HONA and SPCX are new filers with too few quarters for a TTM.
- Non-US listings, foreign private issuers (20-F, 40-F), IFRS figures.
- EBITDA (D&A tagged unevenly, above), so no EV/EBITDA; return on invested capital (it needs a
  tax rate and invested capital, neither tagged consistently).
- **Market cap of a multi-class company** (GOOGL, META): its count is tagged per class, which
  companyfacts leaves out, so it has none; the diluted average is never used in its place.
