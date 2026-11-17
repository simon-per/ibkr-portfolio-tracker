# Company financials — SEC EDGAR, `company_financials`, `/api/company-financials`

> **This file is authoritative for its subsystem.** Keep it accurate in the same change that
> moves the code.

The Company tab shows one company at a time: revenue, operating income, net income, diluted EPS,
free cash flow (before and after stock-based compensation, in total and per share), the diluted
share count, gross, operating, net and FCF margins, buybacks and dividends paid, cash and debt, and
dividends per share, quarterly, TTM or by fiscal year, over 5 years, 10 years or everything filed,
with a key-figures strip at the newest TTM point. **For every held US filer and the Nasdaq-100**
(owner's request, 2026-10-25), held or not. **Sourced from SEC EDGAR**, and since 2026-11-16 —
last, fill-only and proven against SEC — **Alpha Vantage** for an interior gap SEC's layers left
(*The source waterfall*); never Yahoo, never IBKR.

| piece | file |
|---|---|
| the SEC endpoints, pacing, retries | `backend/app/services/edgar_client.py` |
| companyfacts → rows (every rule below) | `backend/app/services/company_financials_normaliser.py` |
| a filing's instance → the dividend per share and diluted EPS companyfacts leaves out | `backend/app/services/edgar_filing_facts.py` (table `sec_filing_reads`, migrations `a7c9e1f3b5d8`, `e7a9c1d3f5b8`) |
| the pass: scope, CIKs, back-off, writes | `backend/app/services/company_financials_sync.py` |
| TTM, FCF, margins, CAGR, YoY | `backend/app/services/company_financials_service.py` |
| tables | `backend/app/models/company_financials.py` (migration `d1e3f5a7b9c2`) |
| reads | `backend/app/routers/company_financials.py`, `backend/app/schemas/company_financials.py` |
| first fill | `backend/app/cli/fetch_company_financials.py` |
| Alpha Vantage's statements, last and fill-only (*The source waterfall*) | `backend/app/services/company_vendor_fill.py` (table `vendor_statements`, migration `a2c4e6f8b0d1`) |
| the one door to Alpha Vantage and its daily budget, shared with the price fallback | `backend/app/services/alpha_vantage.py` |

## Sources

- `https://www.sec.gov/files/company_tickers.json` — ticker → CIK. Asked at most once a pass,
  and only when a held security or a constituent ticker has no answer on record (or the SEC
  listed none and `CIK_RECHECK_AFTER`, 30 days, has passed): `security_ciks` keeps a held
  security's answer, `sec_tickers` a constituent ticker's (GOOG's CIK is stored nowhere else).
- `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json` — every XBRL fact a company
  has filed, every period, every filing. One request per company per week.
- `https://data.sec.gov/submissions/CIK##########.json` — the company's SIC code and its
  description (the overview's industry) and its fiscal year end (`MMDD`), stored on
  `sec_companies` (`sic`, `sic_description`, `fiscal_year_end`; migration `e8c2a4f6b1d3`). The
  document's filing list is read past and dropped. **At most monthly a company**
  (`PROFILE_RECHECK_AFTER`, 30 days, on `submissions_checked_at`), after the figures in the same
  Sunday pass, so a refusal there leaves the figures whole: the first Sunday asks every company
  once, then about one in four a week. A 404 or a document stating nothing is an answer (stamped,
  stored as NULL); a failure stamps nothing and warns (*The industry of N companies could not be
  read*). The run reports `profiles_due`, `profiles_fetched`, `profile_errors`. **Its filing list
  is read for a filing companyfacts skipped**, only when the figures show one and at most monthly
  a company (*When companyfacts lags the filing list*, below).
- `https://www.sec.gov/Archives/edgar/data/<cik>/<accession>/index.json` and the filing's
  XBRL instance beside it — *Filing instances* below — and, for a company whose own concepts are
  read, its label linkbase (`_lab.xml`, *The source waterfall*). Once per filing, ever. The
  `index.json` listing is EDGAR's directory service, which answered 503 to most asks for an hour
  on 2026-11-16 while every document and every filing's `<accession>-index.htm` page answered:
  after its bounded retries `EdgarClient.filing_index` reads that page and takes the files it
  links in the filing's folder (`_listing`, `tests/test_edgar_filing_index.py`). Both failing,
  that company's reads stop for the pass and resume next week.
- `https://www.alphavantage.co/query` — `INCOME_STATEMENT`, `BALANCE_SHEET`, `CASH_FLOW`, last
  and fill-only (*The source waterfall*, *Alpha Vantage, last*), through `alpha_vantage.py` only.

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
below). **It also carries only facts tagged without a dimension** — the dividend per share many
companies tag only on a column of the statement of equity, which *Filing instances* reads from
the filing instead.

### Filing instances

**Why** (2026-11-07, from MA's chart): the TTM dividend per share had a hole wherever one
quarter was missing, and 26 of the 62 dividend payers had quarters with dividends paid and no
dividend per share. The dominant cause was the tagging: MA (2019 to mid-2023), PCAR, INTC, MDLZ,
XEL and COST tag the figure only on a dimension member — `StatementEquityComponentsAxis`
(`CommonStockMember` or `RetainedEarningsMember`, the equity statement's column),
`StatementClassOfStockAxis` (BKR's `CommonClassAMember`), `DividendsAxis`
(`DividendDeclaredMember`, COST) — so companyfacts never had it. The undimensioned total
(`DividendsCommonStockCash`) is there, but over any share count it missed the tagged figure by
more than 0.4–0.9% for half the quarters and 3–4% for a tenth (measured over 1,400): no stand-in.

**How** (`edgar_filing_facts`, `CompanyFinancialsSync._read_filings`): after a company's
companyfacts is normalised, each quarter with dividends paid and no dividend per share (newest
first) names the filing that reports it — the 10-Q whose own period ends there, the 10-K for a
fourth quarter, the original before an amendment (`reporting`). Its `index.json` names the
instance (`<name>_htm.xml` for inline XBRL, else the one `<prefix>-<YYYYMMDD>.xml`), and
`parse_instance` keeps the dividend-per-share facts on **exactly one whitelisted us-gaap member**
(`MEMBERS`); two members giving one period different values leave it out whole. A gap a read
filing already covers (its comparatives: last year's quarter, the year-to-date rung, a 10-K's
three years) is not asked for in the same pass. **A gap its own filing does not fill asks the
filings that repeat it** (`sources`): the one reporting the same quarter a year later, and for
a fiscal year the 10-K two years later too. BKR's 2017–2019 filings tag the dividend on two
members (not read) and its later ones on one; the first production fill skipped the 2019 10-Qs
because newer filings' comparatives had covered their own quarters, and BKR, INTC and PCAR
kept three gaps each that the survey had filled (2026-11-07).

- **Fill, never replace**: the normaliser takes the stored facts beside companyfacts' under their
  own filing's accession number (so the split vote and the latest-filing rule treat them as that
  filing's), and only for a (concept, period) companyfacts has no fact for.
- **Once per filing, ever** (`sec_filing_reads`): a filing never changes, so what it held is
  stored, nothing included, and never asked again for what its reader looked for (the EPS
  reads below added one more look); a 404 index or a filing with no single
  instance is stored as nothing. A failure stamps nothing and stops that company's reads for the
  pass (`filing_errors`, a warning); a 401/403/429 writes the company's figures, then stops the
  pass. A document that is not XML is stored as nothing, with a warning (`filings_unreadable`).
- **Bounded**: two requests a filing, at most `FILING_READS_PER_COMPANY` (24) a company a pass.
  The survey (every gap filing of the 102 companies, 2026-11-07) was 256 filings and about
  860 MB; with the comparatives the first pass reads fewer. After it, a company that keeps
  tagging only on a member (BKR) costs one filing a quarter. A company paying only preferred
  dividends (TMUS before 2018) has gaps too: its filings are read once and hold nothing.
- **Members not read**: a second share class, special dividends, subsequent events, two
  dimensions, typed members, and a company's own extension members (AMAT's per-quarter ones).
  The 2009–2013 filings hold no dimensioned dividend at all.

The survey's effect on the 102 companies: gaps 280 → 134 with the zero rule below, no other
metric's row moved, no existing dividend per share changed but MCHP's FY2011 Q4 (0.173 → 0, its
two dividends declared in Q3 now sum to the year). MA, MDLZ, XEL and COST have no gap left;
PCAR one, INTC two, BKR four.

**The diluted EPS the same way** (2026-11-08). BKR's stored EPS stopped in 2022, so its P/E did
too: companyfacts holds its 10-Ks' undimensioned EPS to FY2024 and no 10-Q's after Q3 2022 —
the 10-Qs print it under "Class A Common Stock" (`StatementClassOfStockAxis` /
`CommonClassAMember`, read off the Q3 2026 10-Q's rendered statement), with no diluted count
to derive it from. So a quarter with net income and no diluted EPS (`eps_gaps`) is a gap too,
asked through the same `sources`, and the instance's EPS-chain facts on a share class
(`EPS_MEMBERS`: `CommonStockMember`, `CommonClassAMember`) are kept — **only when every share
class the filing tags that period's EPS on gives one value**, the classes not read included.
Alphabet tags its three classes alike (kept); a company whose classes earn different amounts
per share (Berkshire's A and B) has no one EPS, whichever class is quoted (left out). The
diluted count then comes from net income / EPS (*Diluted shares when not reported*).

- **What a read looked for** (`sec_filing_reads.reader`, migration `e7a9c1d3f5b8`): 1 the
  dividend alone, 2 the EPS too (`READER`, `READ_SINCE`). A filing an EPS gap names that was
  read at 1 is read once more and replaced whole; a dividend gap never re-reads it. A filing
  read for one gap covers another only through facts of that gap's concepts (`GAP_CONCEPTS`).
- **Measured** on the public API (2026-11-08): 12 of the 102 companies have quarters with net
  income and no EPS, 60 quarters in all — BKR 20 (2016's shell quarters have no filing of their
  own), TSLA 11 (2010–12), ALNY 5, FANG and FAST 4, GOOG and REGN 3, HONA, NXPI, ONDS, RKLB and
  SNDK 2. Each names a filing once ever; BKR then costs one filing a quarter, as its dividend does.
- **A filing's own period** (`filings`, 2026-11-09): the newest end with more than half the
  duration facts of the commonest. The commonest alone named BKR's Q2 2026 10-Q a Q2 2025
  filing (102 comparative facts against 101 of its own), so its quarter had no filing: its EPS
  and dividend per share were never read, which had been put down to companyfacts lacking it.

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
  10-Q/10-K/20-F figure this reads (a 40-F filer, a fund), is asked again on
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
foreign private issuer listed in New York (ASML, NU) gets its CIK and its **annual 20-F figures**
(*Annual-only filers*, below).

**The Nasdaq-100** — `CompanyFinancialsSync.universe`: the `UNIVERSE_SIZE` (100) heaviest
distinct companies in the stored basket of each fund in `UNIVERSE_FUNDS` (`QQQM`, resolved through
`EtfBasketRepository.fund_isins`, the same answer the logo pass's `--basket` reads). The evening
basket refresh keeps it current, so a reconstitution reaches the tab without a code change; the
weights are the index's modified market caps, so "top 100 by market cap" and "the Nasdaq-100" are
the same list (101 lines, GOOG and GOOGL one company; all 101 resolve to SEC tickers, checked
2026-10-25). Equity rows only, **whitelisted** (`INVESTED_ASSET_CLASSES`): QQQM's money-market
fund, futures and cash lines are no companies — and not `counts_as_invested`, whose fund marker
would admit the money-market fund. Foreign private issuers in the index (ASML, AZN, PDD, ARM, …)
get their CIK and then a miss, like a held one. A line QQQM publishes with a CINS and no ISIN
(Linde, Seagate) takes the ISIN the identity store gives its share-class FIGI (`_isins_of_figis`),
or `/companies` carried no ISIN for it and a look-through row of Linde — which only ISINs name —
linked nowhere (review item R5).

**Named elsewhere** — `CompanyFinancialsSync.named_elsewhere` (review item R5, 2026-11-08;
**owner's approval 2026-11-07** for the requests, CLAUDE.md rule 1): 37 of the look-through's top
100 rows linked nowhere. On top of the index's capped hundred (`Constituent.capped`), the pass takes
the look-through's heaviest `NAMED_LOOKTHROUGH` (100) companies **with a US ISIN**, under the
ticker the stored baskets most often give that ISIN, and the watchlist's US tickers (no exchange
suffix). A US ISIN rather than any ticker the SEC lists: a foreign line's ticker can name another
company there (Roche's ROG is Rogers Corp's). A held company is in scope already; one named twice
is one company. Measured budget, approved: about twenty more companies, so about twenty more
companyfacts requests a week, about twenty more Yahoo weekly-close requests every Sunday
(`company_price_sync`), and the Yahoo profile rotation about 20% longer (`company_profile_sync`).
They are stamped like the index's, so every reader of the stamp (the picker, the price pass, the
profile chunk) takes them with no further change. Irish-domiciled 10-K filers outside the index
(Accenture, Medtronic, Eaton) carry no US ISIN and stay without a page (accepted).

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
| revenue | `RevenuesNetOfInterestExpense` → `Revenues` → `RevenueFromContractWithCustomerExcludingAssessedTax` → `…IncludingAssessedTax` (CRWD, ODFL) → `SalesRevenueNet` → `SalesRevenueServicesNet` (ODFL before 2018) → `RegulatedAndUnregulatedOperatingRevenue` (XEL from 2019) |
| net income | `NetIncomeLoss` → `NetIncomeLossAvailableToCommonStockholdersBasic` → `ProfitLoss` |
| diluted EPS | `EarningsPerShareDiluted` → `EarningsPerShareBasicAndDiluted` (ALNY to 2021) → `IncomeLossFromContinuingOperationsPerDilutedShare` (ABNB; MNST after 2011), then net income / diluted shares (below) |
| operating cash flow | `NetCashProvidedByUsedInOperatingActivities` → `…ContinuingOperations` |
| capex | `PaymentsToAcquirePropertyPlantAndEquipment` → `PaymentsToAcquireProductiveAssets` → `PaymentsForCapitalImprovements` → `PaymentsToAcquireOtherPropertyPlantAndEquipment` (LLY) → `PaymentsToAcquireOtherProductiveAssets` (VZ, ROP from 2019) → `PaymentsToAcquireOilAndGasPropertyAndEquipment` → `PaymentsToAcquireOilAndGasProperty` (FANG) |
| SBC | `ShareBasedCompensation` → `AllocatedShareBasedCompensationExpense` |
| diluted shares | `WeightedAverageNumberOfDilutedSharesOutstanding` → `WeightedAverageNumberOfShareOutstandingBasicAndDiluted` (ZS, WDAY, PANW, DDOG); in a loss period the reported basic count (below); then net income / EPS |
| operating income | `OperatingIncomeLoss` |
| dividends per share | `CommonStockDividendsPerShareDeclared` → `CommonStockDividendsPerShareCashPaid` |
| cash | `CashAndCashEquivalentsAtCarryingValue` → `CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents` → `…AtCarryingValueIncludingDiscontinuedOperations` (MDLZ 2016–2018) |
| cash and short-term investments | `CashCashEquivalentsAndShortTermInvestments` (MSFT, GOOGL, ADBE) |
| short-term investments | `ShortTermInvestments` → `MarketableSecuritiesCurrent` → `AvailableForSaleSecuritiesDebtSecuritiesCurrent` → `AvailableForSaleSecuritiesCurrent` |
| gross profit | `GrossProfit` |
| cost of revenue | `CostOfRevenue` → `CostOfGoodsAndServicesSold` → `CostOfGoodsSold` |
| buybacks | `PaymentsForRepurchaseOfCommonStock` |
| dividends paid | `PaymentsOfDividendsCommonStock` → `PaymentsOfDividends` → `PaymentsOfOrdinaryDividends` |
| debt, total | `LongTermDebt` → `LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities` |
| debt, noncurrent | `LongTermDebtNoncurrent` → `LongTermDebtAndCapitalLeaseObligations` → `LongTermNotesAndLoans` (ORCL from 2026) → `ConvertibleDebtNoncurrent` → `ConvertibleLongTermNotesPayable` (PANW, DDOG, ZS) |
| debt, current | `LongTermDebtCurrent` → `DebtCurrent` → `NotesPayableCurrent` (ORCL) → `ShortTermBorrowings` (NFLX) → `LongTermDebtAndCapitalLeaseObligationsCurrent` (ODFL, CSX) → `ConvertibleNotesPayableCurrent` → `ConvertibleDebtCurrent`; else zero when the balance sheet shows none (below) |
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
  that never tagged any (AVGO) holds cash alone. No debt tagged is no net cash, never an assumed debt
  of 0 — **unless the balance sheet itself shows none** (`_no_debt_at_all`, below): MNST, ISRG
  and ANET now have net cash. Enterprise value = market cap less net cash; EV/EBIT over TTM
  operating income, both in the key figures only.
- **An investments concept outside the chain is named, never read** (`_unchained_investments`,
  review item CO8, 2026-11-08). KLAC's net cash was cash alone: its marketable securities sit in a
  concept no chain reads. When the newest balance sheet has cash and neither chained concept, the
  normaliser names every us-gaap concept shaped like a current investment (`INVESTMENT_LIKE`: marketable,
  held-to-maturity, available-for-sale or debt securities, or investments, `…Current`, not
  restricted) with a positive value there; the pass records `unchained_investments` and warns with
  company and concept. Whitelisting it into `short_term_investments` is a one-line change once
  someone has seen what it holds. **KLAC's, read on the server (2026-11-08, owner-approved), is
  `AvailableForSaleSecuritiesDebtSecurities`** — 3.2B beside 1.5B of cash — a total, with no `Current`
  suffix, so this check would not have named it. It is the chain's last link (`AFS_TOTAL`), counted only
  for a period with no non-current investments tagged beside it (`_total_only_when_current`, which
  reads `noncurrent_investments` for that test alone and never stores it): a total with nothing
  non-current beside it is all current, while beside one it would count long-dated securities as liquid.
- **The chains were widened from a measurement, not a guess** (2026-11-15, after the owner
  reported figures missing on the Company tab). Every quarter of the last ten years that a served
  figure lacked, across the 132 companies on production, was matched against the concepts the
  company did tag for that period. The concepts added above are the ones that continued a
  series with the right magnitude (VZ's capex 17–23B a year exactly where
  `PaymentsToAcquireProductiveAssets` stopped; LLY's 7.8B in 2025). Each goes **last**, so it only
  fills: rerun over all 132 documents, no figure that existed changed except share counts (the
  next two items) and NBIS (the reporting currency, below). `tests/test_company_financials_gaps.py`.
- **A count tagged for basic and diluted alike is the diluted count**, and **in a loss period the
  reported basic count is the diluted one** (ASC 260: every potential share is anti-dilutive in a
  loss, so the two are equal by definition). TTWO tags only the basic count in its loss quarters
  from mid-2025. Never in a profitable period, and never a basic quarter differenced from two
  year-to-date averages (TTWO's FY2014 Q4 came to 121M between 112M and 79M).
  `WeightedAverageNumberOfSharesOutstandingBasic` is also the split witness; it is read as the
  internal `basic_shares` metric and dropped before rows are written. Where it replaced net
  income / EPS the reported count moved the figure under 2% (rounded EPS), except before an IPO
  (ZS, MRNA: up to 40%, the estimate being distorted by preferred stock).
- **A current-debt half of zero where the balance sheet shows no current debt**
  (`_no_current_debt_line`, source `no current debt on the balance sheet`). Debt is the total, else
  the halves added, and a company with no current maturities tags no current line: META, ABNB,
  TTWO, PYPL and WDAY had no debt, so no net cash, in most quarters. The zero is read off the
  filing: the newest filing reporting the noncurrent half at that date reports nothing current and
  debt-like there, under **any** concept (`CURRENT_DEBT_LIKE`; a lease alone does not count, debt
  and leases together do). companyfacts carries no company-specific concept, so a current portion
  tagged under one is invisible: **the maturity table is the witness**. A company whose table states
  principal due within twelve months (`DUE_SOON_SHARE`, 2% of the noncurrent half) on any date
  that would be zeroed gets no zero anywhere. Measured: of 661 candidate zeros, the 36 a table
  covered were contradicted 24 times, all at PYPL (current notes in a `pypl:` concept, wrong) and
  ORLY (classes notes due within the year as noncurrent, refinanced through its revolver — right,
  but indistinguishable); both are withheld, leaving 588 zeros at 54 companies.
- **Bridging: the chains extend themselves** (`_bridge`, `BRIDGE_FAMILIES`; owner, 2026-11-15:
  "this should be closed in the script, not one by one"). The weekly pass already re-reads every
  company's whole history; what left gaps was the reading — a hand-kept list of concept names per
  figure, so a company that moved a line to another standard concept had its series stop until
  someone added it. Now, for every company and every flow figure and cash, each us-gaap concept
  outside the chains that is *named* like the figure (`BRIDGE_FAMILIES`: a pattern, and one for what
  it must not be named) is read like a chain concept and **proves itself**: on the periods both
  report it agrees with the chain's figure within 0.5% (`BRIDGE_TOLERANCE`), at least twice and on
  90% of them. Only then does it fill the periods the chain lacks; it never replaces a figure. A
  part named like the whole (`RegulatedOperatingRevenueGas`) never agrees; a concept that never
  overlaps proves nothing. With the hand-added concepts above taken out again, it rediscovers XEL's
  revenue, ROP's and LLY's capex on its own (VZ's two capex concepts share one period only). Over
  the 132 companies it fills 216 more periods of history (JNJ, MNST and WDC's pre-2018
  `SalesRevenueGoodsNet`; ALNY's and FANG's pre-2017 revenue; CSX's and TXN's buybacks). Each pass
  records what filled what in its summary (`bridged`: "JNJ revenue <- SalesRevenueGoodsNet (n)") —
  information, not a warning. Debt is not bridged: a total agrees with its noncurrent half whenever
  nothing is current, so agreement proves less there.
- **A balance sheet with no borrowing at all has no debt** (`_no_debt_at_all`, source `no debt on
  the balance sheet`, both halves 0). ANET, ISRG, PLTR, MPWR, TER and MNST carry no debt and tag
  none, so their net cash — the figure that matters most for them — was unknown in every quarter.
  The zero is read off the filing whose **balance sheet** is at that date
  (`LiabilitiesAndStockholdersEquity`: a cash figure alone can be a cash flow statement's opening
  balance, and 2009–2010 quarters read debt-free beside billions of notes until this was keyed to
  the balance sheet). It fires only when, at that date, the filing states nothing debt-like under
  any concept (`DEBT_LIKE`, a maturity table included; securities held and a facility's capacity
  are not borrowings), and three witnesses agree:
  - **interest**: no material interest (`INTEREST_SHARE`, 0.2% of the balance sheet a year,
    annualised from periods of a quarter or more — MPWR tags stub periods) for a period ending within
    `INTEREST_WINDOW_DAYS` (200) of the date, in any filing — PCAR states interest only yearly;
  - **a borrower companyfacts is blind to** — material interest and no debt figure ever (BRK and PCAR
    report debt per segment, which companyfacts leaves out; banks) — gets no zero up to its last
    material interest (BRK tags interest only from 2025);
  - **no zero between debt** within `DEBT_GAP_DAYS` (400) on both sides: a balance sheet does not
    shed its debt for a quarter (FAST's 2015 line of credit, LIN's 2018, AMZN's 2011). Years without
    debt between two borrowings stay (META 2013–2021).
  Measured over the 132 companies: 668 zeros at 40 companies, none between real debt; Apple before
  its 2013 bonds, NVDA before 2013, Visa before 2015, META 2013–2021, MSTR before 2020, Fortinet
  before 2021 — each a known debt-free period.
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
  nine-month rung gives Q4. **Both rungs of one filing vintage** (`_as_of`, review item CO6,
  2026-11-08): a shorter rung a newer filing restated is taken as the longer rung's filing knew it.
  HON's next 10-Q put the prior nine months on continuing operations while the year stood
  whole-company until the next 10-K, and their difference was a 57% operating margin. Until that
  10-K the year's quarters mix bases (Q1–Q3 restated, Q4 not); each is still a real quarter.
  A reported discrete quarter beats a derived one. Flows and per-share
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
  of exactly zero stays absent unless its ladder shows it** (`_shows_zero`, `_accounted`):
  before 2020 IBKR filed no discrete quarter and tagged 0.10 on every rung (BKNG its 2024 rate
  the same way), which cannot be told from a quarter without a dividend, and a 0.00 must not
  stand in for the unknown. A zero is kept when the ladder is cumulative — its largest rung at
  least `NOT_CUMULATIVE_RATIO` times its smallest (INTC declares twice a year: 0.63 at three
  and six months, 1.26 at nine and twelve) — or the company reported a quarter of that year as
  0 (WMT declares the year in its first quarter and tags the second 0), **and** the rung below
  the zero equals its quarters, each as reported where it was: ADP's FY2016 10-K tagged
  FY2015's 1.95 on the nine-month context against the reported 0.48 + 0.49 + 0.49, and the
  zero fourth quarter that made was never declared (the year less the three quarters gives the
  0.49). Without the rule INTC and WMT had no TTM at all. The quarters are the declared basis,
  so a company declaring two dividends in one quarter (GEHC's Q3 2025, HPE's Q1 FY2016) shows
  that quarter double and another at 0; the TTM sums them right.
- **One rate on every rung** (`_flat_rate_years`, 2026-11-07). The per-share facts alone
  cannot tell IBKR's 0.10 on its three-, six-, nine- and twelve-month contexts from an annual
  declarer's; the dividends declared in money can (`DIVIDEND_TOTALS`, read as a witness and
  never stored, like the basic share count): IBKR declared 7M, 14M, 21M, 28M. A fiscal year
  whose every dividend-per-share fact is one rate (within `FLAT_RATE_TOLERANCE`, 2%: BKNG's
  9.60 over its 25:1 split is 0.384 beside the restated 0.38) and whose quarters of declared
  totals are each within `FLAT_TOTALS_TOLERANCE` (25%; IBKR tags 7M or 8M in millions) of
  their mean paid that rate each quarter. **So does a year's tail**, from a quarter to the
  year end: IBKR's 2025 is 0.0625 in the first quarter and 0.08 after (its April–December
  context too, 35M, 36M, 36M declared). A quarter takes the rate only where it has no reported
  figure — one derived from two rungs is the difference of two rates (BKNG's 0.004 FY2025 Q4)
  — **and only where a fact of the rate ends inside it**: MA's 2021 is 0.44 from its second
  quarter with 434M, 434M, 476M declared (within the tolerance), and its fourth quarter's raise
  to 0.49 is only in the year's 1.81, no rate, so it is never overwritten. A filed year that
  carried the rate becomes the four quarters, for a whole year only (IBKR's 0.40, CEG's 1.41).
  Two quarters of totals are enough, so a year in progress counts (BKNG's 2026, months before
  its 10-K). An annual or semi-annual declarer's quarters of totals are four or two times the
  mean, or nothing: they stay as the ladder rules leave them. Closed IBKR's years to 2025,
  BKNG, CEG's fourth quarters and AMAT's 2012 and 2014–2017 (offline over the 102 companies:
  gaps 134 → 91, no other metric's row moved).
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
(ONDS tags some 10-Ks and Q1 10-Qs in thousands) — **on the raw facts, before any quarter is
derived** (`_rescale_share_slips`, review item CO4, 2026-11-08). The check ran on the finished values,
after Q4 had come from the year still in thousands (negative, so dropped), and ONDS had no TTM share
count since Q3 FY2023. A derived quarter's count lying between its two reported neighbours is kept
however far from each it stands (a quarter of heavy issuance).

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
value) and `fetched_at`. **A period never reported has no row; nothing is stored as 0.** A
dividend per share read from a filing's instance carries its concept's name like any other; its
filing is in `sec_filing_reads` (cik, accn, form, filed, `facts` as JSON, read_at, reader).

### One key for every regulator's company

Since 2026-11-17 (migration `c4e6a8b0d2f4`) a company has a **key**, `<scheme>:<native id>`:
`sec:<cik>` for an SEC filer, `dart:<corp_code>` for a Korean company (OpenDART), `lei:<LEI>` for a
European one (an ESEF report). An SEC filer stays on `sec_companies` and its CIK-keyed tables,
untouched; another regulator's company is a row of **`companies`** (a surrogate `id`, `scheme`,
`native_id`, unique together), and its figures are `company_financials` rows carrying
`company_id` instead of `cik` — a CHECK holds every row to exactly one of the two, and each has its
own unique (company, period end, fiscal period, metric). `models.company_key` is the one spelling
of a key; `SecCompany` and `Company` expose the same attributes the service reads (`key`, `cik`,
`company_id`, `industry`), so one `_priced`, one `key_stats`, one `build_series` serve both, and
the repository's `rows_of` / `coverage_of` / `_replace` pick the column (`_owned_by`).

- **Why not re-key the CIK tables onto a surrogate id** (the alternative): eight tables and about
  150,000 rows rewritten, and every query of the weekly EDGAR pass, the price pass, the profile
  chunk and the vendor fill touched, for SEC filers that gain nothing — a CIK is already a stable,
  unique id. **Why not a second figures table** for the other regulators: two tables of one shape
  with two writers is the drift docs/failure-modes.md catalogues; one table and one writer
  (`_replace`) is not.
- **Measured on a production snapshot** (2026-11-17): upgrade 2.6 s, 151,011 rows and their value
  sum unchanged, integrity and foreign-key checks clean; downgrade drops only `company_id` rows.
- **No Yahoo for another regulator's company** (owner, 2026-11-17): the price pass and the profile
  chunk take only companies with a CIK, and `_priced` gives one no closes — not even its held
  listing's daily close — so its P/E, market cap and every multiple stay absent.

## Service and API

`GET /api/company-financials/companies` — companies with stored figures, one per company: the held
SEC filers, the newest pass's Nasdaq-100 and named companies (above), and each other regulator's
companies of its newest pass: `key, cik, security_id, security_ids, symbol, name, isin, held,
currency, first_period_end, last_period_end, last_fetched_at` (`cik` null beyond the SEC). A held
SEC filer is named by its CIK-mapped listing with the lowest symbol (`security_id` set); any other
by its own ticker, name and ISIN (`security_id` null; the ISIN finds its logo). `security_ids`
names **every** held listing (2026-11-07), so a table row of any of them can link to the company —
GOOG and GOOGL are both Alphabet. **Since 2026-11-17 that includes every held listing the identity
store folds with the company** (`_linked_listings`, `identity_service.group_companies`: ISIN, LEI,
share-class FIGI, `ISSUER_OVERRIDES`), never a ticker: 2330@TWSE opens TSMC's page (filed by its
ADR, TSM), ASML@AEB ASML's, 000660 SK Hynix's. A fold reaching two listed companies links neither.
It is a link only: the page's figures, closes and per-share basis stay its own (TSM's EPS is per
common share in TWD, one ADR is five shares; 2330's TWD close is never put beside it). `held` is
whether any listing is held.

`GET /api/company-financials/{ref}?period=quarterly|ttm|annual&range=3y|5y|10y|max` — `ref` is the
key or, as every link was before 2026-11-17, a bare CIK; an unknown scheme or a malformed id is 404
(defaults `ttm`, `10y`): the same identity fields (`key`, `cik`, ...), then `currency, period, range, as_of,
warnings, key_stats, profile, series` (`profile` and `series.price`: *The overview* below), each series `{points: [{period_end, label, value, yoy}], cagr, yoy}` — every point
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
`tests/test_company_financials_contract.py`.

`GET /api/company-financials/dividend-track` (review item FD4, 2026-11-08):
`{security_id: {dps_cagr_5y, raise_years, payout_ratio}}` for every held listing of a company with
stored figures, read by the Dividends table's *5Y*, *Streak* and *Payout* columns
(`dividend_track`). The 5-year growth is `growth_at` over the fiscal years' dividends per share;
the streak (`raise_streak`) counts fiscal years in a row, newest back, that paid more than the
year before, and a year without a figure, a missing year or a flat year ends it — null when the
newest year paid nothing, never a streak of 0; the payout ratio is `key_stats`' own. A listing
with no US filer behind it (a fund) is absent. Declared before `/{cik}`, which would take the
path. Public reads, rate-limited like every `/api/` read;
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
  **Debt** is the tagged total, else the noncurrent and current halves, and the halves when the
  "total" is smaller than they are — one half under the total's concept (review item CO7: AMD's
  FY2021 read 1M against 313M of notes); `_debt`, `tests/test_company_debt.py`.
  **`investments`** is served beside them (review item VI1, 2026-11-08): what net cash counts
  beside cash (the liquid sum less cash), drawn on the Cash & debt card, so cash + investments −
  debt = net cash on every point; the card showed AMD's cash 5.1B, debt 3.2B and net cash 9.9B.
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
- **A split the filings restated for and Yahoo never booked** (`filed_splits`, review item CO1,
  2026-11-08): the normaliser emits each change of split factor between two consecutive filings
  as `{after, by, ratio}` (the last filing on the old basis, the first on the new), stored as
  `sec_companies.filed_splits` with the rows. `eps_basis` treats one as booked when a Yahoo split
  of its ratio (±2%) falls within 14 days of that interval; otherwise the closes up to `after`
  are divided by its ratio and the closes inside the interval are left out (either basis — a
  multiple there is absent). HON's 1:2 at its 2026 Aerospace separation halved every earlier
  P/E. A single filing voted off its neighbours both ways is a mis-vote, not two splits. NULL
  until a company's next facts fetch writes it.
- **CAGR** over the range's first and last known values; null when the start is not positive,
  the end is negative, or they are under a year apart. **YoY** = the latest point against the
  point a year before it (±20 days, for 53-week years); null when that point is missing or not
  positive. **3/5/10-year CAGRs** (`cagrs`) compare the latest point with the one that many years
  before it (same slack) over the whole history, not the range; null also when the latest is
  negative (a root of a negative ratio is no rate). **All are null for the margins** (lead's
  decision, 2026-10-25): a growth rate of a ratio reads as a figure nobody means.
- **Medians** (`medians`, review item R3, 2026-11-08): a valuation multiple's own median over
  the last 3, 5 and 10 years, positive values only, on the whole history like `cagrs`; null with
  fewer than three values in a span or a history shorter than it, and null for every other
  series. The Valuation card shows them where a growth card shows its CAGRs. **Each span ends
  at the newest point, whether or not it has a value** (2026-11-08): anchored at the last
  multiple, a company whose P/E stopped kept a median of years before — BKR's 3Y chip read
  2019–22 and MRNA's 2020–23 on their 2026 pages.
- **The latest quarter** (`latest_quarter`, review item R4, 2026-11-08): the newest quarter on
  its own — label, filing date, revenue, diluted EPS and FCF against the same quarter a year
  before (the YoY rule), the operating margin's change in points — the overview's row of what the
  newest report said, which TTM blends into three older quarters; tagged `new` for a week.
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

**The weekday filings check, Monday to Saturday 05:00** (2026-11-07,
`COMPANY_FILINGS_CHECK_HOUR`/`_DAYS`; job `company_filings_check`,
`CompanyFinancialsSync.check_filings`). The Sunday pass alone left a 10-Q up to a week off the
tab: on 2026-11-07 AMD still ended at Q2 FY2026 with its Q3 10-Q filed on 2026-11-04, and the
key figures' P/E disagreed with Fundamentals' (Yahoo, daily). The check watches a company in the
weekly pass's scope while its next filing is due — figures stored, the newest period end between
`FILING_EXPECTED_AFTER` (105 days: the quarter and the two weeks the quickest filers take) and
`FILING_WATCH_UNTIL` (`FIGURES_STALE_AFTER` + 30 days) old, not backed off, not fetched within
`CHECKED_WITHIN` (20 hours) — and asks its `submissions` document for the period its newest 10-Q
or 10-K reports (`EdgarClient.latest_report`; amendments and transition reports count, by
the one `edgar_client.PERIODIC_FORMS` the normaliser and the filing reads use too — review item
DR6). Only a period at least `NEW_PERIOD_MARGIN` (14
days) past the stored one fetches companyfacts, through `_one` like the weekly pass (filing
instances included); a `reportDate` a few days off a stored period end must not refetch every
day. Estimated, not yet measured: about a third of the ~100 companies in the window during an
earnings season, one small request each, then none until the next quarter. No ticker list and no CIK lookups (the
weekly pass keeps those). Its own `sync_runs` type, `company_filings_check` with
`company_filings_result`, because `_new_findings` compares a weekly pass with the previous
weekly one and a check's partial lists must not stand in for it; it raises no finding warnings.
Both jobs share one runner (`SchedulerService._edgar_job`). `tests/test_company_filings_check.py`.

**When companyfacts lags the filing list** (review item CO5, 2026-11-08). Six companies (two held)
sat on their Q1 2026 for 221 days: their Q2 10-Qs were filed and listed in `submissions`, and
companyfacts never carried them; inside the window the check refetched companyfacts every
weekday (about sixty fetches that moved nothing) and past it never looked. Three parts, all in
`tests/test_company_filing_fallback.py`:

- **The filing's own instance.** `latest_report` returns the newest periodic filing
  (`PeriodicFiling`: period, form, accession number, filing date). When the normalised rows stop
  `NEW_PERIOD_MARGIN` short of it, `_one` reads that filing's instance once ever
  (`_filing_behind`, `parse_instance(whole=True)`): every chain concept's undimensioned facts,
  balance-sheet instants included, besides the dividend members. They are stored in
  `sec_filing_reads` like any filing read and only fill: once companyfacts carries the period, its
  facts win (`_filing_facts`, which now takes an instant for an instant concept). An
  undimensioned fact wins over a member's for the same period inside one instance.
- **The re-ask guard.** A check fetch that still stops short of the listed period records a lag
  (`record_lag`) on the facts miss stamps, counted on from the count before the fetch (the write
  clears them), so the next ask waits 1, 2, 4… days on the shared doubling (`miss_backoff`) and
  the Sunday pass honours it too. The summary lists the companies as `lagging`.
- **Past the window.** Beyond `FILING_WATCH_UNTIL` the check asks a company once a week, on
  `CHASE_WEEKDAY` (Monday), until `FILING_CHASE_UNTIL` (400 days): past a year of missing
  10-Qs it has stopped filing. Not on Sunday: the weekly pass's tests count every URL, and a
  date-relative ask there would start failing them once the fixture aged.
- **A filing it skipped, older than the newest — automatic since 2026-11-16** (STATUS 7a,
  `CompanyFinancialsSync._read_skipped`, `tests/test_company_skipped_filings.py`). `_filing_behind`
  looks at the newest filing the weekday check is told about, alone. AEP's and EXC's Q2 2026
  10-Qs (filed 2026-07-30) never reached companyfacts although their Q3s did, so Q2's net income
  was derived from the YTD ladder and its diluted EPS (not additive) stayed empty, and with it
  the TTM P/E; they were read by hand on 2026-11-11. Now every fetch (the weekly pass and the
  check) asks the figures whether companyfacts skipped a filing (`edgar_filing_facts.skipped`),
  in three signs: a quarter end within `SKIPPED_LOOKBACK` (730 days, the last eight quarters)
  the figures carry that no filing reports as its own period — neither one companyfacts names
  (`filings`, us-gaap or ifrs-full) nor one already read (`read_period`); a quarter missing
  between two recent ones (more than `MAX_QUARTER_DAYS`, 120, apart); figures older than
  `STALE_QUARTERLY` (150 days), or `STALE_ANNUAL` (a year and 150 days) for an annual-only
  filer. On a sign it asks the filing list (`EdgarClient.periodic_filings`, `submissions`), **at
  most once in `LISTING_RECHECK_AFTER` (30 days) a company**, stamped on
  `sec_companies.filings_listed_at` (migration `f1b3d5e7a9c2`) whatever it holds, and reads
  whole, once ever, every periodic filing listed within the lookback that companyfacts carries
  **no statement fact** of (`companyfacts_accns`: us-gaap or ifrs-full — TSMC's FY2025 20-F was
  in companyfacts with two cover-page facts and none of its figures), newest first, within
  `FILING_READS_PER_COMPANY`. They only fill, like every filing read. A sign nothing answers (a
  new filer's comparative quarters, HONA, SPCX; a company that stopped filing) costs one small
  request a month. The run records `filings_listed` and `skipped_filings` ("AEP 10-Q
  2026-06-30"). **Measured over the 132 companies (2026-11-16): 11 lists asked, 8 filings read**
  — AEP's and EXC's Q2 and Q3 2026, ALNY's Q1 2026, NU's and TSMC's FY2025 20-F, SPGI's FY2024
  10-K — and no figure that existed changed. SEC stays the one source: the gap is in
  companyfacts' index, not in what the SEC holds.

**What each pass checks** (owner's request, 2026-11-07): the Sunday pass tests the stored rows
of **every** company in scope after its loop, fetched or not, and the weekday check the companies
it fetched. Until review items CO2/DR3 the Sunday lists covered only the companies that pass
fetched, so one the weekday check had fetched (not due on Sunday) dropped out of that Sunday's
record and the next Sunday warned about it again as new. The record
(`company_financials_result` in `/api/scheduler/history`) lists
`ttm_dividend_gaps` — a dividend payer whose newest four quarters miss a dividend per share,
so its TTM is absent now — and `stale_figures` — newest figures older than
`FIGURES_STALE_AFTER` (150 days; a 10-Q is due 40–45 days after its quarter, a 10-K 60–90
after the year, so the newest period end is at most about 135 days old). **A warning names
only the companies the previous scheduled run did not list**: a gap nothing can close (WDC's
2013–2015 dividends, tagged nowhere) is a line of the
record every week, not a banner the reader learns to skip. The first run after the deploy
warns about all of them once.

**First fill** (inside the container; the CLI's own `single_flight` fences its own process only):

    docker exec backend-portfolio-backend-1 python -m app.cli.fetch_company_financials --dry-run
    docker exec backend-portfolio-backend-1 python -m app.cli.fetch_company_financials

`--symbol NVDA` (repeatable; a held symbol or a constituent's ticker) narrows it, `--force`
ignores staleness and back-off. It records a `manual_company_financials_fetch` row.

## Prices

The P/E's closes come from Yahoo (`company_price_sync`, CLAUDE.md rule 1; enabled by the owner
2026-10-25): **one history request per company a week**, about a hundred, after the EDGAR pass.

- **Scope**: the SEC filers the tab lists (`list_companies`, `cik` not null), by CIK, asked by the SEC's ticker
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

## The overview

The Company tab opens on an overview of **the company itself** — never a portfolio figure
(owner, 2026-11-07; docs/frontend.md *The Company tab*). Beyond the figures above it reads:

- **Industry and fiscal year end** from the SEC's submissions document (*Sources*).
- **Yahoo's company-level figures** — analyst rating counts, price targets, forward P/E, PEG,
  forward revenue and EPS growth, the next earnings date and its EPS estimate, the business
  summary. **A held company reads its listing's own rows**, kept by the daily held pass:
  `fundamental_metrics` (which keeps `business_summary` from the `.info` it already reads, at no
  extra request), `analyst_ratings`, `earnings_events`. **A company nobody holds** reads
  `company_yahoo_profiles` (migration `f1a3c5e7b9d2`), filled a chunk a run by
  `company_profile_sync` inside the 05:00 `fundamentals_refresh` (CLAUDE.md rule 1, approved by
  the owner 2026-11-07 for that budget; docs/flex-and-sync.md has it). Both through the same
  readers — `fundamentals_service.extract_metrics` and `extract_earnings`,
  `AnalystRatingService.fetch_counts` — so one company's figures never have two parsers. Until the
  rotation reaches a company (two weeks for the first fill), its Yahoo figures are absent.

**`profile`** on `/api/company-financials/{cik}` (`CompanyFinancialsService._profile`; pinned both
ways by the contract test): `industry, fiscal_year_end, description`; the price line `price,
price_date, return_1m, return_1y, return_5y, return_10y, week52_low, week52_high`; `forward_pe,
peg_ratio, fwd_revenue_growth, fwd_eps_growth, rating {strong_buy … strong_sell, consensus},
analysts, target_low, target_mean, target_high, target_upside, yahoo_currency, next_earnings_date,
next_eps_estimate, yahoo_as_of`. Rates are fractions; the unknown is null. `rating` is
`analyst_rating.rating_of` — null when all five counts are zero, as on the positions table —
and its `consensus` `analyst_rating.consensus_of`, the rule every rating label in the app goes through;
`yahoo_currency` is the targets' and the EPS estimate's (Yahoo's quote), which need not be the
filings' `currency`.

- **One resolver** (`_yahoo_figures`): a held company's listing rows or the chunk's row, never a
  mix; `tests/test_company_overview_profile.py` pins which wins. **The counts are the company's
  everywhere** (`app/services/company_yahoo.py`, review items DR1/DR2, 2026-11-08): every held
  listing of a company with a CIK shows its canonical listing's counts (the lowest symbol, the one
  this overview reads), so GOOG and GOOGL rows match the Alphabet page; `counts_of` is the one
  reading of the five counts off a row. `tests/test_company_yahoo_family.py`.
- **A split mid-week holds the daily close back** (`_split_since`, review item CO3, 2026-11-08):
  when IBKR recorded a price-restating split of the held listing after the newest weekly close, the
  daily close is on the new share basis and the weekly closes still on the old, and appended to
  them it read as a −90% week and a tenth of every multiple. The last weekly close stands until the
  Sunday pass refetches the history.
- **The price line is the market's, the multiples' basis is the figures'.** `_closes` reads the
  weekly closes and the held listing's newest daily close once; the P/E and its kin take them
  through `_on_eps_basis` (a split after the newest filing scales them), the price line and
  `series.price` take them as quoted — a 10:1 split would otherwise show ten times the price.
  `price_line`: the newest close and its date; the 1M change against the newest close on or before
  a month back (within `PRICE_MATCH_DAYS`), 1Y by `yoy`, 5Y and 10Y by `cagrs` (annualised); the
  52-week low and high of the closes — weekly closes, not intraday prices.
- **The upside** is the mean target over the newest close, only when the target's
  `data_currency` is the closes' currency and the close is within `PRICE_MAX_AGE_DAYS`.
- **`series.price`** (`price_series`): the market closes shaped like any series, the range counted
  back from the newest close, each point's YoY and the CAGRs by the same rules, `label` = the date.
  The Period toggle does not apply.

## Peers

`GET /api/company-financials/{ref}/peers` (`CompanyFinancialsService.peers`, review item R1,
2026-11-08): the companies the tab lists (`list_companies`'s scope) in the company's SEC industry
(`sec_companies.sic`, filled by the Sunday pass's `submissions` read), and when that holds fewer
than `PEERS_MIN` (4) others, its three-digit group (`sic // 10`, `widened`). The company first,
then the rest by TTM revenue, largest first, at most `PEERS_MAX` (15) rows. **Each row's figures are
the overview's own** — `_priced` (rows, closes on the split basis, today's close) and `key_stats`,
the one path `get_financials` reads too; `tests/test_company_peers.py` compares every peer's
`key_stats` with its own overview's. No industry is no peers. On the tab: a collapsed card under the
charts (`CompanyPeersCard`), shown only when the profile names an industry, that asks the server
only once opened (about six queries a peer); the industry and `SIC 3674` (or `SIC 367x` widened)
on its header, a sortable table (`peerColumns`, in `tableFamily.test.tsx`) whose rows link through
`CompanyLink`. Figures stay in each company's reporting currency, as everywhere on the tab.

## Screener

`GET /api/company-financials/screener` (`CompanyFinancialsService.screener`, 2026-11-08): every
company `list_companies` lists, one row each, in that order — the overview's key figures
(`_priced`, `key_stats`), the company's own dividend record (`dividend_track`, handed the
`key_stats` it already has rather than recomputing them), and today's P/E, P/FCF and P/S against
**their own 3-, 5- and 10-year medians**: `multiple_medians` takes `build_series`' two steps on
the TTM view (each TTM point valued at its period end's close) and the one `medians`, so a row's
`pe_median` (`{y3, y5, y10}`, likewise `pfcf_`, `ps_`) is the Valuation card's chips on that
company's page, and `*_vs_median` is `premium` for each span — today's over the median, less one,
null when either is missing or not positive (a loss year's P/E has no history to be read against).
Every span in one answer, so the card's switch asks nothing. `tests/test_company_screener.py`
compares every row with its own page.

- **Kept server-side** (2026-11-08): reading every listed company's figures took 8–9 s on the VPS
  (2.5 s for 120 synthetic companies locally). The answer is kept in the process
  (`_screener_cache`, one uvicorn worker) under `_screener_key`, every input a row reads that a
  write can change: today (a close ages past `PRICE_MAX_AGE_DAYS`); `list_companies` itself
  (identity, held listings, membership, the figures' coverage and fetch stamp, so a refetch moves
  it); each company's `prices_fetched_at` (every price write stamps it, splits included) and
  industry; the held listings' newest daily closes, value included (a provisional close is
  rewritten in place); and their price-restating corporate actions (`_split_since`). The key is
  read before the figures, so an answer is never older than its key. Nothing expires on a timer:
  an unmoved key has nothing new to read. A hand edit of the database bypasses it until the next
  restart or write. The client still keeps the answer 30 minutes (`companyScreenerQuery`) and asks
  only once the card is opened. `tests/test_company_screener.py` writes each kind of input once.
- **On the tab:** a collapsed *Screener* card at the top of the Company tab (`CompanyScreenerCard`)
  with All | Held and 3Y | 5Y | 10Y (the median span, 5Y first), opening on the cheapest against
  their own P/E median (ascending, blanks last); the sort stays on its column across spans.
  Columns (`screenerColumns(span)`, in `tableFamily.test.tsx`): market cap, P/E, its median, P/E,
  P/FCF and P/S vs their medians — headed `P/E 5Y`, `vs 5Y` and so on for the span — revenue and
  EPS 5-year CAGR, FCF margin, ROE, dividend yield, raise streak. A row links to the company's page
  through `CompanyLink`.

## Annual-only filers

Review item R6 (2026-11-08). One companyfacts read each on the server that day: **ASML** files its
20-F in US GAAP (623 us-gaap concepts, no ifrs-full); **NU** and **TSMC** in IFRS (`ifrs-full`,
151 and 334 concepts; NU in USD, TSMC in TWD with a USD convenience translation of the latest year).
All three were misses: the normaliser read 10-K/10-Q facts in us-gaap only.

- **The 20-F is an annual report**: it joins the one `PERIODIC_FORMS` set, and
  `ANNUAL_FORM_PREFIXES` (`10-K`, `20-F`) is the one test of "this filing states a year", read by
  the normaliser and the filing reads alike.
- **IFRS concepts are read under their US-GAAP twins** (`IFRS_TWINS`), so every chain, ladder and
  vote applies unchanged; only exact equivalents (revenue, profit attributable to owners, diluted EPS,
  operating profit, operating cash flow, capex, diluted shares, dividends, buybacks, cash, equity).
  A filer with any us-gaap chain concept is read in us-gaap alone, never both. **A 20-F's own
  instance is read the same way** (STATUS 7b, 2026-11-16): `parse_instance(whole=True)` takes
  the undimensioned `ifrs-full` lines under their twins when the filing has no us-gaap chain
  fact, so a 20-F companyfacts has not taken in fills its year (NU's and TSMC's FY2025, months
  late in companyfacts; `tests/test_company_ifrs_instance.py`). A filing's figure in another
  currency than the company reports in is not read (`_filing_facts`, as `_unit_for` reads
  companyfacts): TSMC's 20-F states its latest year in USD too.
  The company reports in **the currency its newest periodic filing states most of its figures
  in** (`_reporting_currency`), and a concept stated only in another currency is not read
  (`_unit_for`), so two currencies never meet in one series. Until 2026-11-15 each concept kept
  the currency *it* reported most, which kept NBIS (Yandex until 2024) in fifteen years of RUB and
  ended its series at FY2023. TSMC stays in TWD: its 20-F translates only the latest year into
  USD, beside TWD for every year, so its ADR's USD closes match nothing — no multiples, rather
  than wrong ones.
- **An annual-only filer's TTM is its fiscal years** (`_series_points`): each year is its own
  trailing twelve months, so the default view and the key figures show the latest year; the
  quarterly view stays empty.
- They are asked again when their miss back-off runs out (at most 30 days); TSMC is in scope only
  while something names its US listing (held as 2330 on the TWSE, it is not a US listing).
- The weekday check treats a 20-F like any periodic report: from 105 to 180 days after a fiscal
  year end it asks such a filer's filing list daily and finds that year already stored — about 75
  small requests a filer a year, accepted rather than a second cadence for three companies.

## The source waterfall

Owner, 2026-11-16: the Company tab closes its own gaps, source by source, **SEC first and Alpha
Vantage last**; every layer only fills what the layers above left empty and never replaces a
figure; every filled value names its source (`source_concept`); a figure the company never
reported stays blank — no vendor estimate, no zero, no derived stand-in (ABNB's capex, LLY's and
JNJ's operating income, the banks' FCF). Yahoo is not part of it (CLAUDE.md rule 1). In order:

1. **companyfacts** and its chains (*The normaliser*), extended by **bridging** (a standard
   concept that agrees with the chain fills its gaps).
2. **The filings companyfacts skipped** (*When companyfacts lags the filing list*): read whole.
3. **IFRS instances** (*Annual-only filers*): a 20-F's `ifrs-full` lines under their twins.
4. **The company's own concepts, by their labels** (below).
5. **Segment columns summed where no total exists** (below).
6. **Alpha Vantage, last** (below).

The passes record what bridging and layers 2 to 6 filled — `bridged`, `extended`, `segments`,
`skipped_filings`, and the statement fill's own `sync_runs` row — as information, never a warning.

### The company's own concepts, by their labels

companyfacts carries the standard taxonomies only: NVDA tagged its capex FY2013–FY2021 as
`nvda:PurchasesOfPropertyAndEquipmentAndIntangibleAssets`, CDNS its debt as
`cdns:CarryingValueOfDebtOutstanding`, and neither is in companyfacts at all. They are in the
filing: its instance holds the facts, and its **label linkbase** (`<prefix>-<date>_lab.xml`, one
more request beside the instance) names each concept in words.

- **When** (`edgar_filing_facts.extension_gaps`, `CompanyFinancialsSync._read_filings`): a fiscal
  year from 2009 (`EXTENSION_SINCE`) whose rows lack capex, or debt (neither the total nor both
  halves), asks **its own 10-K** — not the later ones that repeat it — once ever, inside the
  per-company cap (`FILING_READS_PER_COMPANY`, 24 a pass), so a long history is read over a few
  Sundays and resumes where it stopped. Every filing read for any gap then reads its label
  linkbase too (`READER` 3: a filing read before is read once more for this gap, never again).
  Only a us-gaap filer that is no bank, broker or insurer (`edgar_client.FINANCIAL_SIC`,
  6000–6411): an IFRS filer's lines are standard ones, and a bank's capex and debt are not the
  industrial figures. Years only: one 10-K holds a year's flows and its balance sheet; a quarter's
  10-Q is not asked, and a Q4 then comes from the year less three quarters as before.
- **Which concepts** (`EXTENSION_LABELS`, `extension_metrics`): the company's own concept (not
  us-gaap, dei, srt) whose **standard label** fits one metric's pattern and not its exclusions —
  capex ("Purchases of property and equipment…", "Capital expenditures"; not businesses, proceeds,
  leases, accruals), a debt total ("Carrying value of debt outstanding", "Total debt"; not
  principal, face or fair value), a current or noncurrent half ("Current portion of long-term
  debt"; "Long-term debt, net of current portion"). A label fitting two metrics names none.
  Undimensioned, one currency (the company's), a flow as a duration and a balance as an instant
  (`parse_instance(extensions=…)`); stored in `sec_filing_reads` with `metric` and `label`.
- **The proof** (`company_financials_normaliser._extend`): a label is a name, the figures are the
  proof, as in bridging — on every period the concept and the chain both report it agrees within
  `BRIDGE_TOLERANCE` (on `BRIDGE_MIN_SHARE` of them; a debt total against the two halves added where
  no total is tagged). Where they never overlap — the reason the concept is needed at all — the
  label alone names it. Only fills; source the concept (`nvda:PurchasesOf…`). `tests/test_company_extension_concepts.py`.
- **PYPL's current notes are not an extension concept** (read 2026-11-16): its 10-Ks tag the
  current portion as `LongTermDebtCurrent` on `LongtermDebtTypeAxis`/`SeniorNotesMember` only,
  and since its Q3 2026 10-Q undimensioned (1.396B at 2025-12-31, 2.333B at 2026-09-30), which
  companyfacts now carries. Its older quarters stay withheld by the maturity-table witness
  (*The normaliser*).

The measurement over the 132 companies is in *Known gaps and out of scope*.

### Segment columns summed where no total exists

BRK and PCAR state their debt per segment — Insurance and Other / Railroad, Utilities and Energy;
Truck, Parts and Other / Financial Services — which companyfacts leaves out, so their debt was
unknown and `_no_debt_at_all` rightly withheld a zero.

- **Whose** (`edgar_filing_facts.debt_blind`, `CompanyFinancialsSync._reads_segments`): a us-gaap
  filer with no debt figure from any other source in any period (a debt-free balance sheet's
  zeros count as figures). Over the 132 companies that is BRK and PCAR alone.
- **When** (`segment_gaps`): every period end within ten years (`SEGMENT_LOOKBACK`) without debt
  asks its own filing, once ever, within the per-company cap; every read of such a company keeps
  its segment columns (`parse_instance(segments=True)`: the `SEGMENT_DEBT` and
  `SEGMENT_WITNESSES` facts on exactly one explicit member, the witnesses' undimensioned totals,
  and the names of every borrowing a member shows). No extra request: the instance is read anyway.
- **The proof** (`company_financials_normaliser._segments`): summed only when the members
  **provably partition the company** — a witness (assets, liabilities) states its total and its
  members on the same axis add up to it within `BRIDGE_TOLERANCE` (BRK: 917.8B + 236.1B = 1,153.9B
  of assets at 2024-12-31; PCAR: 21.0B + 22.4B = 43.4B). Then each member of that partition either
  states one total-debt concept (`SEGMENT_DEBT`: `LongTermDebt`, `…CapitalLeaseObligations
  IncludingCurrentMaturities`, `DebtAndCapitalLeaseObligations` — BRK's —,
  then `DebtLongtermAndShorttermCombinedAmount` — PCAR's Financial Services: its contractual
  borrowings, 15,946.7M at 2024-12-31, 0.3% above the balance sheet's 15,895.0M, which PCAR tags
  only under its own `pcar:` concept; carrying amounts first), the same for all, or
  shows no borrowing under any concept (PCAR's truck column: it owes nothing); a member showing a
  borrowing the concept does not state refuses the sum whole, and so does a partial view (a parent
  company's column, one segment of three). Otherwise the debt stays blank. Fills `debt_total` only
  where no debt figure exists; source `segments:<axis>`. `_no_debt_at_all` still treats such a
  company as one whose debt companyfacts cannot see. `tests/test_company_segment_debt.py`.

The measurement over the 132 companies is in *Known gaps and out of scope*.

### Alpha Vantage, last

`INCOME_STATEMENT`, `BALANCE_SHEET` and `CASH_FLOW` (`company_vendor_fill`), after every SEC layer.

- **Which gaps** (`interior_gaps`): a recent quarter (the newest eight, `RECENT_QUARTERS`) where
  SEC's rows lack a figure **while reporting it in an earlier and a later quarter** — a hole, not a
  line the company stopped or never started reporting. ABNB's capex since 2023 is a trailing gap
  and stays blank: a vendor's number there would be an estimate of a line the filing does not
  have. The fields (`FIELDS`): revenue `totalRevenue`, operating income `operatingIncome`,
  operating cash flow `operatingCashflow`, capex `capitalExpenditures`, cash
  `cashAndCashEquivalentsAtCarryingValue`, the debt halves `currentLongTermDebt` and
  `longTermDebtNoncurrent`. No diluted share count: Alpha Vantage carries none.
- **The proof, per company and metric** (`_proven`): on the periods SEC also reports (quarters and
  years, matched within a week), Alpha Vantage agrees within `BRIDGE_TOLERANCE` at least
  `BRIDGE_MIN_AGREEMENTS` times and on `BRIDGE_MIN_SHARE` of them; otherwise none of it is used for
  that metric. Its `"None"`, `0` and a missing field are no figure; a report in another currency
  than SEC's rows is not read; no bank, broker or insurer (`FINANCIAL_SIC`). Source
  `alpha_vantage:<field>`, `filed` the day it answered.
- **Asked** (`CompanyVendorFill.run`): daily, right after each EDGAR job (the Sunday pass and the
  weekday check, `SchedulerService.company_vendor_fill_job`, under `EDGAR_GATE` — no new slot, the
  05:00 hour is already in `ALL_SYNC_HOURS`), only the statements a company's gaps need, stalest
  ask first. A company at most once a week (`sec_companies.vendor_checked_at`), longer on the
  shared doubling while its asks fill nothing (`vendor_miss_count`, `miss_backoff`); a backlog waits
  for the next day's budget. The budget is the one `alpha_vantage` keeps for every caller: 25 calls
  a UTC day less the price fallback's reserve of 10 (docs/flex-and-sync.md). What it answered is
  kept (`vendor_statements`, the mapped fields only; migration `a2c4e6f8b0d1`), and every rewrite
  of the company's rows — the weekly EDGAR fetch included — proves and applies it again, so SEC's
  newer figure always wins. Its own `sync_runs` row (`company_vendor_fill`, body
  `company_vendor_result`): `asked`, `filled`, `unproven`, `spent`, `capped`, `budget`. Without a
  key the job records nothing. `tests/test_company_vendor_fill.py`.

Nothing is asked from this session: the key lives only on the VPS. Its reach, estimated on the rows layers 2 and 3 leave: 89 interior gaps in recent quarters at 26 companies, mostly debt halves, 26 calls to ask them all — under two days of its budget.

## Classification

Owner, 2026-11-16: every company is classified by a waterfall, **SEC first and every other source a
fallback**, never by hand or by a per-company table (`company_classification.py`). Two answers,
each served with the source that decided it on the overview's `profile` (`business_model`,
`business_model_source`, `sector`, `sector_source`), computed at request time from what the stores
hold, so a basket refresh or a profile read shows the next time the page opens.

**The business model** — `industrial`, `bank` (banks and brokers) or `insurer` — decides which
figures apply (an industrial's FCF, debt and operating margin mean nothing for a balance sheet that
is lending and borrowing):

1. **the statements** (`statement_model`, stored on `sec_companies.statement_model` by each
   companyfacts fetch): which of a model's own concepts the newest annual report tags
   (`STATEMENT_CONCEPTS`) — a bank's deposits, net interest income and non-interest income and
   expense, a broker's customer payables, three of them; an insurer's premiums, claims reserves
   and policy liabilities, two. An insurer is tested first. No us-gaap annual report (an IFRS
   filer, a new filer) decides nothing.
2. **the SIC code** (`sic_model`): 6000–6299 bank or broker, 6300–6411 insurer, else industrial.
3. **SEC's reviewing office** (`ownerOrg`, `sec_companies.owner_org`, kept from the submissions
   document the pass already reads monthly; migration `b3d5f7a9c1e2`): an office that reviews
   operating companies says industrial; Finance and Crypto Assets review banks, payments companies
   and brokers alike and say nothing.
4. industrial. The fund baskets' GICS is never used here: it puts Visa, Mastercard and PayPal in
   Financials, and they are not banks.

**The sector**, in the app's eleven (`sector_taxonomy`):

1. **the SIC code**, through `SIC_SECTORS` — SEC's published code list translated range by range,
   never a company; a catch-all code (`SIC_CATCH_ALL`: 7389 "services, not elsewhere classified",
   blank checks) decides nothing.
2. **the reviewing office**, where its name is one sector (Finance, Life Sciences, Technology).
3. **the fund baskets' GICS**: the commonest sector the stored baskets give the company's ISINs
   (`gics_sector`), the Look-through's own source.
4. **Yahoo's sector** of a held listing. 5. `Unknown`.

A lower source naming another answer is a **conflict**, recorded and never resolved by hand: the
weekly pass lists the SEC-level ones in `classification_conflicts` (statements against SIC against
office), information, not a warning.

**Measured over the 132 companies** on SEC's sources alone (2026-11-16): 5 banks and brokers — BAC,
JPM, IBKR and SOFI by their statements (BAC and JPM tag six of the bank concepts, IBKR three), NU by
its SIC (an IFRS filer) — 1 insurer (BRK, by its reserves' changes: it tags premiums and claims per
segment, which companyfacts leaves out), 126 industrials. Industrials tagging one bank-like line
(`InterestIncomeExpenseNet` at AMD, ADSK, ROST; a loan-loss provision at PCAR's finance arm, IDXX)
stay industrials; MSTR's SIC is 6199 (finance) and its statements an industrial's, which win.
Sectors: 124 from the SIC code; the 8 others are the catch-all 7389 — Visa, Mastercard, PayPal,
MercadoLibre, DoorDash, PDD, Alibaba, Kaspi — and fall to the baskets' GICS on production.

**SEC first has a known cost**, accepted by the owner: SIC codes are assigned at registration and
rarely revised. Alphabet and Meta are 7370 (computer programming), so Technology where GICS says
Communications; PACCAR 3711 (motor vehicles), Cintas 2320 (apparel) and Fastenal 5200 (building
materials retail) are Consumer Cyclical where GICS says Industrials; Airbnb 7340 Industrials, S&P
Global 7320 Industrials, ADP 7374 Technology, MicroStrategy 6199 Financials; ASML and Lam 3559
(special machinery) Industrials, where SEC's own reviewing office (Technology) and GICS disagree —
both recorded as conflicts. `tests/test_company_classification.py`.

### A bank's figures

The `bank` model's own KPI set (2026-11-16), the cards the Company tab draws for it instead of the
industrial's cash & debt (a bank's deposits and borrowing are its business, not leverage):

- **Chains** (`METRICS`): `net_interest_income` (`InterestIncomeExpenseNet`), `noninterest_income`,
  `noninterest_expense`, `credit_loss_provision` (`ProvisionForLoanLeaseAndOtherLosses` →
  `ProvisionForLoanAndLeaseLosses` → `ProvisionForLoanLossesExpensed`), `deposits` (`Deposits`),
  `loans` (`LoansAndLeasesReceivableNetReportedAmount` →
  `FinancingReceivableExcludingAccruedInterestAfterAllowanceForCreditLoss`: BAC and JPM moved from
  the first to the second with CECL). Flows sum into a TTM, deposits and loans are balances.
- **Derived** (`company_financials_service._derive`): the **efficiency ratio**, non-interest
  expense over net interest plus non-interest income (lower is better; no growth rate), and
  **book value per share**, equity over the shares outstanding at the period end (a count tagged
  per class, IBKR's or BRK's, gives none). Served as series like any other.
- **Measured** over the 132 companies: adding the chains added rows only — 886 net interest income,
  310 non-interest expense, 612 provisions, 258 non-interest income, 232 loans, 201 deposits — and
  no figure that existed moved. BAC, JPM, SOFI and IBKR carry net interest income, non-interest
  income and expense in all of their last eight quarters, the three banks deposits and loans; a
  provision only JPM lately (BAC stopped tagging one undimensioned after 2022, SOFI never did);
  IBKR, a broker, has no deposits or loans. An industrial tagging one of these lines (AMD's net
  interest income) has the rows and no card. `tests/test_company_bank_figures.py`.
- **No insurer set yet**: BRK, the only insurer in scope, tags its premiums and claims per segment
  only, so companyfacts holds none; it keeps the industrial cards and gains book value per share
  where a single count exists. An insurer set (premiums earned, losses and expenses over premiums)
  waits for an insurer that tags them.

## Markets beyond the SEC (planned, owner 2026-11-16)

Today a company has a page only if it files with the SEC (US filers, and foreign filers of a 20-F
through their US listing: ASML, TSMC, NU, BABA, PDD, ARM). Xetra, Euronext and SIX listings, and the
look-through's non-US companies, have none. The plan keeps this page's rule — **the company's own
regulator first**, every other source a fill-only fallback — market by market, in the owner's order:

| order | market | the regulator's source | what it gives | notes |
|---|---|---|---|---|
| 1 | EU, UK | ESEF annual reports, collected at filings.xbrl.org, found by **LEI** | every EU-listed annual report since 2021 in iXBRL, `ifrs-full`; half-years only where filed voluntarily | read through `IFRS_TWINS` as a 20-F is; ISIN → LEI from GLEIF, which the identity pass already caches; annual figures, so TTM is the fiscal year |
| 2 | South Korea | OpenDART (the FSS), free key | structured statements, quarterly, K-IFRS; a Korean industry code | |
| 2 | Taiwan | MOPS (the TWSE's filing system) | XBRL, no official API | fragile; TSMC is already a 20-F filer |
| 3 | Japan | EDINET (the FSA), free key | XBRL for every listed company, J-GAAP or IFRS; the TSE's 33 sectors | needs a J-GAAP concept map |
| later | China A/H, India, Middle East | CNINFO/HKEX, NSE/BSE/MCA21, Tadawul | no usable free structured source today | their US ADR filers only |

Switzerland's SIX issuers are outside ESEF; only those that file voluntarily would have figures.
Every figure stays in the company's reporting currency (*Why the reporting currency*), each source is
a new upstream with its own bounded budget, and Alpha Vantage's free statements are effectively
US-only. Classification works for the non-US companies before their figures do: with no SEC data
they fall through to the fund baskets' GICS sector (STATUS.md, *Worth doing next* 7).

## Known gaps and out of scope

**The source waterfall, measured 2026-11-16** over the 132 companies: the real pass (`_one`, three
passes in a row, a fresh database, companyfacts as downloaded that day, every filing it asked for
read from EDGAR), against the 2026-11-15 normaliser, without production's dividend/EPS reads. In
the last eight quarters, by layer — **the filings companyfacts skipped and IFRS** (11 filing lists
asked, 8 filings read: AEP's and EXC's Q2 and Q3 2026, ALNY's Q1 2026, NU's and TSMC's FY2025 20-F,
SPGI's FY2024 10-K): revenue 1 → 0, FCF 55 → 52, diluted shares 28 → 27, cash 11 → 10, debt 95 → 94,
operating income 116 → 115, and NU and TSMC reach FY2025; **the companies' own concepts**: 42 rows —
CDNS's debt at 12 year ends (`cdns:CarryingValueOfDebtOutstanding`, 2.476B at 2024-12-31), NVDA's
capex for FY2013–FY2021 and the Q4s it gives (`nvda:PurchasesOfPropertyAndEquipmentAndIntangible
Assets`); no other company's 10-Ks among those read label a capex or debt line of their own that the patterns take;
**segment columns**: 82 rows — BRK's debt in every quarter for ten years (124.8B at 2024-12-31,
129.1B at 2025-12-31), PCAR's at its year ends (15.9B; its 10-Qs state no segment total in a concept this reads); debt
95 → 84 over the two. 343 filings read over the three passes, about 1.2 GB once; 29 reads SEC
refused (its archive answered 503 to most requests for an hour that day) wait for a later pass, as
they would on production. **No figure that existed changed or went.** No layer writes a zero: a
segment column with no borrowing counts as owing nothing only inside a sum the partition proves.
Checked against the filings' text: NVDA's FY2018 10-K prints "Purchases of property and equipment
and intangible assets (593) (176) (86)", the facts read; BRK's balance sheet "Notes payable and
other borrowings 44,885" and "79,877", the two members summed; PCAR's contractual "Borrowings …
$15,946.7", the figure summed (its balance sheet's 15,895.0 is tagged only under `pcar:`).
Alpha Vantage, last: nothing asked from the measurement (the key is on the VPS only); what it may
fill is at most 89 interior gaps in recent quarters at 26 companies, mostly debt halves.

Measured over the 132 companies on 2026-11-15 over the last eight quarters, what the tab shows
first (before → after the chains, bridging and both debt rules): revenue 9 → 1 missing quarters,
FCF 95 → 55, debt 273 → 95 (46 → 23 companies), share counts 30 → 28. Bridging closed none of the
recent ones: **what remains is not in companyfacts under any standard concept** — not filed
separately, or filed only under a company's own extension concept, which companyfacts omits:

- **Operating income is not a line** at LLY, JNJ, IBM, ADP, KLAC, PCAR, XOM and the banks (BAC,
  JPM, IBKR); BKR and COHR stopped presenting one (2024). Pre-tax income is not operating income,
  so none is derived.
- **Capex no longer tagged**: ABNB from 2023 and APP from 2024 fold it into another investing line
  (ABNB's 10-Q for Q3 2026 has no capex line at all, read 2026-11-15), so their FCF ends there.
- **Banks and brokers** (BAC, JPM, IBKR, SOFI, BRK): FCF, operating income and debt in the
  industrial sense do not apply; absent by design.
- **Gross margin** where the income statement has no cost of revenue (utilities, VZ, ORCL, INTU
  since 2020, PYPL, MA…): `CostsAndExpenses` is every cost, not the cost of revenue.
- **Debt left unknown** at PYPL and ORLY by the maturity-table witness above, and at companies whose
  only debt concepts are company-specific.

- **NVDA capex, FY2013–FY2021**: NVDA tagged its annual capex ("purchases related to property
  and equipment and intangible assets") as an `nvda:` extension concept, which companyfacts does
  not carry. Its Q1–Q3 capex exists from the 10-Q ladders up to mid-2020, but every Q4 and every
  year in between is null. FY2023's capex survives only as the nine months and the year (later
  filings' comparatives), so only its Q4 is derived. NVDA's annual FCF has FY2010–FY2012 and
  FY2022 on, and its TTM FCF starts at Q3 FY2024 (checked on production, 2026-10-25). Reaching extension concepts would need each
  filing's XBRL instance, a different source.
- A unit slip in a monetary concept that no newer filing overlaps is not caught (the share-count
  check has NI / EPS to compare with; revenue has nothing).
- **Dividend per share gaps left** (about 90 quarters across 20 companies, 2026-11-07):
  IBKR's three quarters before 2014, whose rungs no declared total vouches for; AMAT before
  2012 and in FY2013, and MELI, AMGN and MAR, tag each declaration on a one-day or
  one-month context, or on AMAT's own members, with slips beside them (AMAT's FY2013 Q4 0.09
  against a 0.10 rate) — summing them would guess; some filings tag no dividend per share
  anywhere (WDC 2013–2015, CTAS's FY2017); 2008–2009 quarters reported only as comparatives
  before XBRL (MA, PCAR, FAST, ROP); TMUS before 2018 and LITE paid preferred or other dividends, not common.
- **KDP before 2018** mixes Dr Pepper Snapple's and Keurig's filings (the 2018 reverse merger moved
  the fiscal year): its 2016-2017 share counts jump. Accepted; the figures since 2018 are one company's.
- **New filers**: HONA and SPCX have too few quarters for a TTM.
- Non-US listings; a 40-F filer; a 20-F filer's quarters (its 6-Ks carry no XBRL figures this
  reads); IFRS concepts without an exact US-GAAP twin (`Borrowings`, so no net cash for NU or TSMC).
- EBITDA (D&A tagged unevenly, above), so no EV/EBITDA; return on invested capital (it needs a
  tax rate and invested capital, neither tagged consistently).
- **Market cap of a multi-class company** (GOOGL, META): its count is tagged per class, which
  companyfacts leaves out, so it has none; the diluted average is never used in its place.
