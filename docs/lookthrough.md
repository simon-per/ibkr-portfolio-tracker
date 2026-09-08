# Look-through — company-level exposure, baskets, identities

> Moved verbatim from `CLAUDE.md` on 2026-09-08, when that file became a short index so a
> session no longer loads ~92k tokens of documentation to start. **This file is
> authoritative for its subsystem**: every invariant here was a bug first. Keep it accurate
> the way CLAUDE.md was kept — update it in the same change that moves the code it describes.

## Look-through — company-level exposure

`GET /api/portfolio/lookthrough?limit=50` → `LookthroughService.get_lookthrough()`, rendered by
`LookThroughTab.tsx` (the 9th tab). Direct holdings plus every held fund decomposed into the
companies inside it, folded across listings and share classes. Pure DB read — no provider, safe
at any hour.

It exists because **a large part of this account sits in ETFs**, so "how much do I own of Nvidia"
was unanswerable — and the *direct* side was already fragmented, because one company routinely
occupies several rows of Positions. The three shapes are all different, which is why this is not
a string match on names or symbols:

| shown as | really | folds on |
|---|---|---|
| `GOOGL@NASDAQ` + `ABEA@IBIS` | Alphabet | **one ISIN** (`US02079K3059`) — no provider needed |
| + `GOOG@NASDAQ` | Alphabet class C | the **LEI** (`US02079K1079` is a different ISIN) |
| `ASML@NASDAQ` + `ASML@AEB` | ASML | the **LEI** (`USN070592100` NY registry vs `NL0010273215` Amsterdam) |

### Grouping is a union, never a precedence chain

`company_identity.company_groups()` unions any two members sharing **any** of ISIN /
shareClassFIGI / LEI / a declared override. The obvious design — `key = lei or share_class_figi
or isin` — **re-creates the bug the feature exists to fix**: when one of a company's ISINs
resolves an LEI and its sibling does not, the first is keyed by the LEI and the second by the
ISIN, so they land in two rows while the response reports a cheerful "1 unresolved". A weaker
identifier had already folded them and the stronger one pulled them apart. The key is chosen
*after* grouping (LEI > shareClassFIGI > lowest ISIN) purely so the API has a stable id.

Order-independence is a property, not an accident: the module is pure and
`test_company_identity.py` shuffles the input 25 times and asserts the grouping and every
display name are byte-identical.

### Both identity providers are required, and they fail in opposite directions

Measured against this account's own ISINs on 2026-08-14, both keyless:

| | folds | has no record for |
|---|---|---|
| **GLEIF** `api.gleif.org/api/v1/lei-records?filter[isin]=` | share classes and multi-ISIN companies (both Alphabet ISINs → `5493006MHB84DD0ZWV18`; both ASML ISINs → `724500Y6DUVHQD6OXN27`) | **TSMC, Samsung, SK Hynix, Credo** — the Asian ordinaries held here |
| **OpenFIGI** `POST api.openfigi.com/v3/mapping` | one share class across venues (GOOGL@US + ABEA@Xetra) | nothing held here; resolves all four GLEIF misses |

So a single-provider design loses either the Alphabet/ASML folds or every Asian ordinary.
`isin_identities` stores both with **separate `*_checked_at` stamps**: a NULL identifier beside a
non-NULL stamp means *asked, that provider has nothing*, distinct from *never asked* (no row).
Without the split, every run re-asks the four permanently-absent ISINs forever — the loop the
holiday rule and `UPSTREAM_RETRY_COOLDOWN_SECONDS` both exist to stop. Separate `*_source`
columns because a bulk-file LEI is not an API LEI, the `source='alpha_vantage'`-as-
`'yahoo_finance'` lesson.

**`compositeFIGI` is stored and must never enter the union.** It is per-exchange-composite, so
it folds nothing the ISIN does not, and unioning on it would merge unrelated lines.

**Neither provider folds an ADR to its ordinary**, and this is live rather than theoretical:
TSMC's ordinary is `BBG001S6Q004` while the `TSM` ADR is `BBG001S5WWW4` (as it must be — an ADR
is a distinct instrument), and GLEIF gives the ADR a LEI while giving the ordinary none. `2330@TWSE`
is held directly and `TSM` is a top constituent of the US-listed funds. Hence `ISSUER_OVERRIDES`
in `app/etf_sources.py` — ISIN-keyed, **reason mandatory by test**, currently one entry. Growth
there is the signal a tier below is being misused. `test_company_identity.py` checks it from
*both* sides: without the override TSMC is two rows, with it one, which is what proves the
override carries the fold rather than masking a broken tier.

**Display name is a separate decision from the key.** GLEIF returns the *legal* name in its own
script — TSMC's is `台灣積體電路製造股份有限公司`, correct and unreadable on a dashboard whose every
other row is Latin — while OpenFIGI's is clean ASCII truncated to ~30 chars and better only for
those. So: Latin-script legal name, else FIGI name, else non-Latin legal name, else the
highest-weight contributing row's name, else the ref. Deterministic, because a label that
changes with sort order is this codebase's signature bug. The Latin test is codepoint-based and
deliberately permissive about accents, so "Société Générale" keeps its own name.

### The partition, and why it is the only assertion that matters

```
direct_equity + looked_through_equity + fund_residual + nested_fund + uncovered_fund
  == total_market_value
```

To the cent. One equality catches a dropped holding, a double-counted constituent, a
renormalisation and a mis-scaled weight alike. `test_lookthrough_partition.py` pins it **and**
fails when the response grows a top-level `*_eur` field classified as neither a bucket nor a
non-bucket — so a bucket added later cannot escape the identity. (`test_allocation_completeness.py`
claims to be family-level and hardcodes its tuple; this is that shape done properly.)

Positions come from `PortfolioService.get_positions_breakdown()` and nowhere else, so the
base-currency projection is inherited and the total **cannot disagree with
`/api/portfolio/summary`** — pinned in `test_api_smoke.py`.

### Nine rules, each a wrong number the other way

- **Nothing is renormalised onto covered value.** Every percentage is a share of the *whole*
  portfolio, so while some funds have no basket every row is an understatement — and rescaling
  would convert a stated gap into a confident lie. Same refusal `rebalance.ts` makes about
  targets. `coverage_pct` leads the panel on the KPI row, **outside any collapsible**, the
  `MonthlyReturnsHeatmap` lesson — and since 2026-09-08 the itemised `warnings[]` sit *inside*
  the collapsed *Fund coverage* card at the bottom, at the owner's request: ten near-identical
  staleness sentences directly under the KPI row pushed the company table below the fold, which
  is the always-present-banner pathology from the other direction. The Coverage footnote names
  every condition present rather than only the most severe, and the collapsed header counts the
  notes, so what is hidden is their text and never their existence.
- **Equity asset classes are a whitelist**, never a blacklist — the reasoning behind
  `get_deposits()` selecting `DEPOSITWITHDRAW` by name. A blacklist admits the next label an
  issuer invents (`Rights`, `Warrant`, `Preferred`) as a company.
- **`asset_class_available=False` is a real state.** Xtrackers/DWS publishes no asset-class
  column at all, so the whitelist cannot run: every row counts, the residual becomes
  `100 − Σ(all rows)`, and the API says the filter did not apply. Never infer "equity" from "has
  an ISIN" — a bond has one too.
- **A basket below `MIN_BASKET_COVERAGE_PCT` (80) is reported as absent**, with its measured Σ.
  A residual is right for EMIM's real 98.04 and catastrophic at Σ=3, where "this fund is 97%
  cash" is a *plausible* figure — the dangerous kind.
- **A constituent that is itself a fund gets its own bucket, not a company row.** Live risk:
  iShares baskets carry the BlackRock ICS liquidity fund as a line item. v1 does not recurse —
  that needs a depth cap and a visited set, because a feeder can hold its own share class.
- **A repeated ISIN inside one basket is summed.** Copying `import_prices.py`'s last-row-wins
  rule for a repeated date would silently drop weight; hence `UNIQUE(fund_isin, line_no)`.
- **The truncated tail is a named row carrying a value**, or the visible percentages sum to well
  under 100 under a "% of portfolio" header. Sort is `value desc, company_key asc`, because
  thousands of constituents tie near zero and without a total order the tail would shuffle
  between requests and a company would appear and vanish.
- **An unvaluable holding is excluded from both sides and named**, on the server-side test
  `market_value_eur > 0` alone — cited from `dividend_service`'s forward-yield filter rather
  than reinvented. An unpriced *fund* is the sharpest silent failure here: it renders not as a
  visible zero but as the **absence** of its companies from rows that still say "% of portfolio".
- **There is deliberately no cost or gain column.** Splitting a fund's cost across constituents
  needs the basket as it stood on each purchase date, which nothing stores — so any per-company
  cost would be fabricated, not merely approximate. Stated in the schema docstring so it is not
  "completed" later.

### The two charts, and why neither is a pie

`lib/lookthroughChart.ts` builds both; `LookThroughCharts.tsx` draws them. The arithmetic is in
`lib/` for the reason `portfolioKpis.ts` is — the interesting mistakes are numeric and testable
in `node` without paying jsdom's startup.

**A treemap for the companies, a stacked bar for the partition, and a pie for neither.** The
company ranking is ~50 rows spanning three orders of magnitude, where comparing angles fails
completely; the partition is three segments with long names, where a horizontal bar reads left
to right at 390px and a ring does not. A pie is only ever right for a part-to-whole of ≤6
roughly-comparable slices, which describes neither.

Four rules, three of which restate rules this feature already has:

- **The treemap's tiles cover the whole portfolio**, so the truncated tail and the unattributed
  remainder are tiles. Drawing only the companies would fill the card with the ~79% that could
  be attributed and render it as the whole — the renormalisation refused everywhere else, this
  time in a form where nobody would notice, since a treemap has no axis to disagree with.
- **The composition bar folds `fund_residual` + `nested_fund` + `uncovered_fund` into one
  segment.** They are one thing to a reader — value no company row accounts for — and three
  categorical hues spent on a distinction the fund table below makes in full is how a chart
  becomes a worse table. The partition still closes; it is summed one level up.
- **The two legends must not share a phrase.** The bar splits *value* while the treemap
  classifies *companies*, so a shared word would mean a share of the book in one and a
  property of a company in the other, forty pixels apart — and Alphabet is in both charts at
  once. Pinned by `test_does_not_reuse_one_phrase_for_two_meanings`.
- **A zero total draws nothing**, not an empty frame. Same refusal as `concentrationPct`.

### Grouping the treemap by sector, and the one prohibition it narrows

Tiles cluster **by sector**, not by whether the exposure was chosen directly. The owner asked
for it, and the direct/indirect split it replaced is still on the page as the composition bar,
where it answers a question about *value* rather than about companies.

Three things make it more than a display change:

- **The sector comes from the issuer basket first and Yahoo second**, measured rather than
  assumed: of the 103 ISINs behind the top-100 rows, BlackRock's baskets classify **97**, DWS's
  34, and `securities.sector` (Yahoo, direct holdings only) **27** — and BlackRock never
  contradicts itself across IWDA/SXR8/EMIM. Where Yahoo and BlackRock overlap they differ only
  by taxonomy, systematically, which is what `app/services/sector_taxonomy.py` exists to
  reconcile: one `normalise()` over four vocabularies, mapping to the eleven names the
  Allocation tab already displays. Unmapped input becomes `Unknown` rather than raising, and
  `Unknown` never votes.
- **A company's sector is a value-weighted majority of its contributing rows, tie-broken on the
  name.** A folded company can be described differently by two funds, and anything
  order-dependent here is this codebase's signature bug — `test_lookthrough_partition.py`
  shuffles the input and asserts the answer does not move.
- **This narrows a documented refusal rather than breaking it.** `etf_basket.py` said `sector`
  was stored and *deliberately unserved*, because serving it would put a second sector answer
  on the Allocation tab. It is served now for **grouping only**: per company, with no
  portfolio-level rollup anywhere in the response, so nothing says "this portfolio is 35%
  tech". The Allocation tab keeps `ETF_ALLOCATIONS` as its answer, and the two are not
  derivable from each other — this one covers only the ~79% that decomposes. The comments on
  both sides say so; `country` stays unserved for its own reason (it is a country, and every
  geographic bucket in the app is a region).

**The palette is the first in this app that is *chosen* per theme rather than one set of hexes
taking its chances against both surfaces.** `--viz-*` in `index.css`, two sets, each run through
a CVD/contrast validator against the surface it actually renders on (`#ffffff` / `#020817`) on
the **all-pairs** pairlist, because a treemap can seat any tile next to any other. Four details
are load-bearing:

- **Only four sectors get a hue; the rest fold into `Other sectors`.** Not a shortcut — the
  all-pairs pairlist is brutal, and brute-forcing all 256 subsets of the eight-slot categorical
  order found that **only four** of them clear it simultaneously (slots 1, 4, 5 and 6). Five
  hues cannot be made to pass by re-stepping. Four covers 68% of this book by value and the
  fold is where the 8-hue ceiling would have bitten anyway.
- **`AllocationTab` and the treemap share `lib/sectorColors.ts`**, so a sector is one colour
  across both tabs. The old `SECTOR_COLORS` failed validation outright: `#3b82f6` Technology
  against `#8b5cf6` Communications measures **ΔE 1.3 deuteran** — the two largest groups here,
  indistinguishable to a deuteranope. `Unknown` also took a *positional* fallback colour and
  moved whenever the chart reordered; it has a fixed grey now, because colour follows the
  entity and never its rank.
- **Every fill carries its own ink.** Dark-mode `--viz-sector-2` measures 2.94:1 against white,
  so one hardcoded label colour makes one group unreadable in exactly one theme — which nobody
  testing the other will see. Re-stepping that swatch to fix the contrast broke its CVD
  separation from green instead (ΔE 4.1), so the *ink* was flipped rather than the fill.
- **`sectorPaint` must consult both the sector record and the structural one.** It looked at
  only the first, so `unattributed` and `other_companies` both fell through to the `Other
  sectors` grey: three meanings, one colour, three identical legend swatches, and nothing
  failed — falling back *is* correct for a genuinely unknown group. Found by sampling pixels
  out of a screenshot, pinned now by a test that fails on the mutant.

**Only depth 2 is drawn, and that is a finding rather than a shortcut.** The first version
painted a frame and a header strip at depth 1 to delimit each sector; neither ever appeared,
because Recharts paints a parent *before* its children and the children tile the parent exactly.
No unit test can see paint order. So the grouping is carried by adjacency and fill, and each
group's name and share by the legend underneath, which nothing can paint over.

The remaining estimate is the tile label's *fit*: SVG text cannot be measured before layout, so
`UPPERCASE_ADVANCE_PX` budgets for the worst glyphs, because IBKR names arrive shouted
(`NU HOLDINGS LTD/CAYMAN ISL-A`). Budgeting for mixed case put `ARISTA NETWOR…` over both edges
of its own tile. Under-filling costs a character; overflowing costs the tile boundary.

### One fundness predicate, keyed on ISIN

`ETF_ALLOCATIONS` entries now declare `"isins": [...]`, and `fund_isins()` /
`is_known_etf_isin()` / `symbol_for_fund_isin()` / `allocation_for_fund_isin()` are **the**
fundness predicates. The live
collision that forced it: this account holds the **UCITS** VanEck fund `IE00BMC38736` (LSE), not
the far better-known US `SMH`, and a symbol-keyed lookup cannot tell them apart. `app/etf_sources.py`
holds only *how to fetch* and is cross-checked against `ETF_ALLOCATIONS` in both directions by
`test_etf_source_registry.py`; a third census of what counts as a fund would be the same
divergence a third time. `_build_isin_index()` raises **at import** on a duplicate ISIN, because
two funds sharing one would silently give one the other's basket.

**`allocation_service` asked it by ticker until 2026-08-17, so the app carried two answers to
one question** — the ISIN way on the Look-through tab and the ticker way on the Allocation tab,
which is this file's opening warning in its purest form. Nothing was wrong on this account when it
was found (every held fund's symbol happened to be unique), and that is what *latent* means here
rather than a reason to leave it: the symbol form is the one that decides a **figure**. Two ways it
bites, in ascending order of how quiet it is — the UCITS `SMH` gets the US fund's sector and region
split (two semiconductor funds, so the numbers stay plausible), and a plain **stock** whose ticker
collides with a row is distributed across eleven sectors as though it were a fund, which is a
fabricated allocation rather than an approximate one. Note the contrast with `currencyExposure.ts`,
which matches funds **by symbol on purpose**: there the output is a stated caveat, and CLAUDE.md
accepts it for exactly that reason. A caveat may be approximate; a percentage may not.

`allocation_for_fund_isin(isin)` is the accessor those three call sites now share — one lookup
resolved once per holding rather than three, because three lookups is three chances for the charts
to disagree about one holding, which they have already done twice. `get_etf_allocation` and
`is_known_etf` survive as lookups over the *table* (the first is what the ISIN accessor is built
on), and `tests/test_fundness_predicate.py` fails any **service** that calls either — the family
form, so the next call site is caught rather than these three.

### Baskets, and DBPG

`etf_baskets` (one row per fund: as-of, source, row counts, weight sums,
`asset_class_available`) is split from `etf_holdings` deliberately — a `SUM(weight_pct)` over
holdings cannot tell "98.04 because the file carries cash and futures rows" from "98.04 because
2% of rows failed to parse", and those need different responses. Denormalising the as-of onto
every row would make "the fund's as-of" a `MAX()` that one stale row ages.

`weight_pct` is a **percent**, matching `etf_mappings.py`, and normalising at the adapter
boundary is mandatory: DWS ships fractions summing to 1.0 and iShares percents summing to ~100,
so a fraction landing here makes the fund contribute 1/100th of its value and read as 99% cash.
`sector` is captured and served to **one** consumer — grouping the look-through treemap, per
company, with no rollup (see *Grouping the treemap by sector* above). `country` remains
unserved: it is a country while every geographic bucket in the app is a region, so using it
needs a hand-maintained country-to-region map. See `etf_mappings.py`'s docstring for the full
precondition for switching the three allocation charts onto either.

**"Stale" is per-adapter (`ADAPTER_STALE_DAYS`), and a single global threshold was wrong.** The badge
is only useful if it means *the issuer has newer holdings we failed to fetch* — Xtrackers and iShares
republish daily, so 7 days there is a missed fetch worth acting on, while Vanguard US publishes
month-end and lags ~6 weeks *by design*, so one 45-day rule badged it permanently for behaving
exactly as documented. A badge that can never clear is the always-present-Flex-banner pathology.
A future quarterly source (SEC N-PORT arrives 75–136 days old) needs its own entry rather than a
raised default. Staleness deliberately **does not reduce `coverage_pct`** — the percentage answers
"how much is attributed", not "how current is it" — which is exactly why the age has to be surfaced
on the card rather than only in the fund table.

**The 18:00 `full_sync` keeps baskets and identities current (2026-09-08); until then nothing
did, and that was the feature's weakest property.** Baskets were populated by a deliberate CLI run
and then decayed in place, and on production the stale-basket warning sat on every market-data run
for two weeks — seven funds, one line each — which is the always-present-banner pathology this file
keeps rediscovering. The data was always one keyless HTTP call away and `etf_baskets` was always the
cache; what was missing was the trigger. `app/services/etf_basket_refresh.py` is that trigger, hung
off the 18:00 job as its last step, and four things about it are load-bearing:

- **It fetches from the detector's verdict.** `stale_basket_verdicts` is the one predicate;
  `find_stale_etf_baskets` formats it into `warnings[]` and the refresh fetches from it. The
  detector's message now says the evening run will fix it and names the CLI only as the fallback.
- **It refuses everything the CLI refuses**, through the same functions: the fetchers and the parser
  dispatch moved to `app/services/etf_basket_fetch.py` and both the CLIs and the job call them. A
  fetch error, a parse failure, a row-count collapse or a backwards as-of each leave the previous
  basket in use and become that fund's warning — never the job's status, and never a half-applied
  basket.
- **It is not on the read path.** `/api/portfolio/lookthrough` stays pure DB: a public GET that
  reached seven third-party sites on a cache miss would be a denial-of-service vector aimed at
  somebody else's servers, and entering `SYNC_PIPELINE` from a read would bump the clock every
  other route's cooldown reads. Once a day, from the job that already holds the gate, matches
  issuers that publish once a day.
- **Identity follows the baskets, bounded.** A re-import clears the CINS/SEDOL resolutions on
  purpose, so the OpenFIGI pass that restores them runs whenever a basket was replaced; and a
  bounded ISIN pass (`SCHEDULED_IDENTITY_LIMIT`, 25 per evening — GLEIF has no batch form) walks the
  held securities and material constituents never asked about, so a newly bought security folds with
  its fund exposure within a day and a rebalance converges over a few. `unresolved_value_eur` still
  shows what is left.

Two clocks matter: the six daily feeds (Xtrackers, iShares, Invesco, First Trust, Defiance, VanEck)
go stale within a week and Vanguard US publishes **month-end with a ~6-week lag** (75 days), so a
normal evening refreshes nothing and a busy one refreshes the six. The CLIs remain the by-hand route
— a file an issuer only publishes by email (VWCE), a first import ahead of the evening, or a fund
whose refresh keeps failing, where the saved body is what makes the failure debuggable.

**A stale basket does warn now** (`SchedulerService.find_stale_etf_baskets`, 2026-08-17), hung off
the market-data job beside its four siblings for the documented reason: those slots succeed while
Flex is refusing. Four rules carry it, and each is wrong the other way round:

- **Held funds only**, mirroring `find_stale_priced_securities`' restriction to open lots — a basket
  for a fund nobody holds moves no figure.
- **The basket a fund actually reads comes from `LookthroughService._alias_proxied_baskets`**, not
  from a second copy of the proxy rule, so the age reported is the age of the file the numbers came
  from. For a proxied fund that is the *source's*, which is also where the threshold comes from.
- **The threshold is `stale_after_days`**, the same per-adapter table the tab badges on. A second
  constant here would be the third definition of "stale", after the one that already badged Vanguard
  permanently.
- **Nothing unclearable warns.** A fund excluded by design, and one whose only route is a hand
  download *with nothing to borrow*, stay silent — the tab's fund table names them instead. But
  **"has a route" follows the proxy**: VWCE's own adapter is `manual` while VT is fetchable, so a
  missing VT is actionable and must not be skipped just because the held fund has no route of its
  own. That was the first draft's bug, and the test is what found it.

Identities still have no detector of their own; the bounded evening pass converges them and
`unresolved_value_eur` shows what is left.

**`as_of_date` is the issuer's own where it publishes one, and the fetch date where it does not** —
and one issuer publishes a date that is *worse* than none. Xtrackers publishes nothing at all: not
in the CSV, not in a `Last-Modified` header (verified). Defiance publishes `Data as of 08/17/2026`
on a file downloaded on the 16th describing Friday the 14th's close — a T+1 **effective** date, the
same convention Invesco makes explicit by shipping `effectiveDate` beside `effectiveBusinessDate`
(and `parse_invesco` takes the business one for exactly this reason). Both cases err the same way:
the true as-of can only be *older*, so a stood-in or forward date makes a basket look **fresher**
than it is, which is the wrong direction for a staleness alarm. So `parse_defiance` clamps to
`min(stated, fetched_on)`, and both cases set `as_of_is_issuer_stated=False` rather than hiding it.

**DBPG carries two independent disqualifiers, and only one of them has been answered.** It is a
synthetic swap-based S&P 500 **2× leveraged** ETF: the 46-name basket it publishes is substitute
collateral (measured top holdings Mastercard 6.6%, Altria 5.7%, Tesla 4.9% — the tell), *and* even
the real index basket understates its exposure by half. `replication` and `leverage` are recorded
**separately** for exactly this reason, and the split earned itself on 2026-08-17: the account owner
asked for DBPG to be decomposed from **VOO's** S&P 500 basket, which answers the collateral
objection and does nothing about the leverage.

So it is no longer `excluded`, and both fields still do work:

- **`replication == "synthetic"` makes the proxy beat its own basket**, not merely fill a gap.
  `_alias_proxied_baskets` skips a stored basket for a synthetic fund, so importing the collateral
  file cannot silently un-proxy it — which is the protection the outright exclusion used to give.
- **`leverage == 2.0` drives a `warnings[]` line** saying its true exposure to each company is
  about double what is shown. It is **stated rather than scaled** because the five buckets must sum
  to the portfolio's market value to the cent, and multiplying one of them breaks the one identity
  this feature rests on. A row reading 0.9% where the truth is 1.8% is a plausible figure, which is
  the dangerous kind, so it cannot be left implicit.

`test_a_synthetic_fund_is_never_decomposed_from_its_own_basket` enforces the first as a family rule
rather than as DBPG's detail, so a second swap-based fund cannot arrive without it. **VOO is declared
in both fund tables but is not held** — it exists only as a basket donor, and its `ETF_ALLOCATIONS`
blocks are pinned identical to SXR8's and DBPG's because all three track the S&P 500.

### Sources, and what still needs a hand download

**Seven adapters, all keyless and login-free, all verified against live files.** Only **VWCE**
has no route at all, and it borrows VT's basket until one exists.

| adapter | route | the thing that bites |
|---|---|---|
| **DWS** | `etf.dws.com/etfdata/export/GBR/ENG/csv/product/constituent/<FUND_ISIN>/` | keyed by fund ISIN, nothing to discover; echoes `ShareClass ISIN` on every row for the parser to check |
| **BlackRock** | a varnish JSON API needing a per-fund `portfolio_id` | several share classes are distinct product ids over **one** portfolio and return byte-identical baskets, so a copy-pasted id is *invisible* — `test_etf_source_registry.py` checks uniqueness |
| **Vanguard US** | profile API, 500 rows/page | as-of lags ~6 weeks, which is normal, not stale |
| **Invesco** | `dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/<CUSIP>/holdings/fund` | keyed by the fund's **own CUSIP**, derived from its ISIN — nothing to discover, unlike BlackRock. The *product page* is a single-page app, which is what an earlier note recorded as "no route" |
| **First Trust** | `ftportfolios.com/retail/etf/etfholdings.aspx?Ticker=<T>` | HTML, and the holdings table is the sixth of eight — `_pick_table` matches on headers, never on position |
| **Defiance** | `defianceetfs.com/<ticker>-full-holdings/` | **not** `/<ticker>/`, whose table is rendered client-side and absent from the response |
| **VanEck** | `vaneck.com/nl/en/investments/<slug>/downloads/holdings/` | XLSX. Needs a **cookie jar** (a cookieless GET loops the consent redirects) and the `/nl/en/` locale **pinned**. Parsed with `zipfile` + `ElementTree`; do not add `openpyxl` for one 5 kB file a week |

**The BlackRock row has a second dimension: `locale` selects the catalogue, and a fund missing
from it 400s rather than returning nothing.** One host serves every domicile, but the default
`en_GB` in `BLACKROCK_PARAMS` only finds the UCITS lines — the US-domiciled **IQQ** answers
`400 BAD_REQUEST_INVALID_PARAM_VALUES` under `en_GB` and returns all 106 rows under `en_US` from
the identical URL. So a US iShares fund declares `params={"locale": "en_US"}` rather than the
adapter growing a second endpoint; the payload shape is identical, which is why `parse_ishares`
has no branch for it. Pinned by a family rule keyed on the ISIN's country prefix, so the next US
iShares fund is caught by declaration rather than by somebody debugging a 400.

**Note also that IQQ's `portfolio_id` happens to equal the id in its product-page URL, and the
UCITS entries' do not** — IWDA's page is `/products/251882/` against `portfolio_id` 287737. Read
it from the sitemap and confirm by row count, as the comment in `etf_sources.py` says; do not
generalise from IQQ.

**All 15 funds decompose, two of them by proxy** (VWCE via VT, DBPG via VOO). Neither of those two
has a usable route of its own: Vanguard Europe publishes VWCE's holdings only by email on request
(month-end + 15 days), and DBPG publishes collateral rather than constituents. The read path never
dereferences `adapter`, so a declared-but-unimplemented one degrades to "no basket yet", never to a
wrong number.

### A borrowed basket, and the three ways it could have become a silent one

`FundSource.basket_proxy_isin` lets a fund with no published basket be decomposed using
another fund's. **Two entries: VWCE borrows VT's and DBPG borrows VOO's**, both at the account owner's
instruction. It is an
approximation stated as one, and four things make it safe to have in a file whose governing
rule is that an unjustifiable figure is absent rather than invented:

- **The judgement is not new here.** `ETF_ALLOCATIONS` already pins VT's regional split to
  VWCE's *deliberately*, with the reasoning written down: FTSE Global All Cap and FTSE
  All-World are the same index family differing by small-cap inclusion. What is new is
  applying it to 10,032 company rows instead of four regions.
- **It errs low, measurably.** The sleeve VWCE omits is almost exactly the part of VT's file
  that carries no weight anyway (8,007 of 10,032 rows are published at 0.00%), and VT's
  percentages are the shared names spread over a *wider* index — so every company figure this
  produces is smaller than the truth and the shortfall lands in the residual. Understatement
  is the direction chosen everywhere else in this feature.
- **It is aliased at read time, never copied into `etf_baskets`.** A copy would be two
  implementations of one basket, drifting the moment the source is refetched — this file's
  opening warning applied to data. One stored basket, two readers.
- **A real import silently wins**, in either order, because the proxy is only consulted when
  the fund has no basket of its own. Nothing has to be un-declared when the file arrives.
- **And the position is converging on its own proxy.** The account owner closed the
  hand-download follow-up on 2026-08-17 rather than leaving it open, because the
  Ireland-domiciled sleeve is being rotated into US-domiciled ETFs for tax reasons — the same
  rotation `get_contributions()`'s splice exists to survive. VWCE is on its way to *being* VT,
  so the approximation narrows over time instead of drifting. Do not re-open chasing the real
  file; STATUS.md records the decision.

It is reported wherever the real thing would be: `proxy_for_symbol` on the fund row (an amber
*Via VT* badge, not a green *Decomposed*), a `warnings[]` line carrying the declared reason
verbatim, and the Coverage card staying **amber** — because "every fund decomposed" over a
borrowed basket is the reassuring-zero failure this codebase keeps rediscovering, and unlike a
percentage threshold this badge can actually clear. Staleness comes from the *source* basket's
adapter, since the age that matters is the age of the file the numbers came from.

`test_etf_source_registry.py` refuses a proxy with no reason, a self-proxy, a chain (two
approximations compounding under one label), a target that is excluded from look-through, and
a target that is itself `manual` — a proxy is only worth having if the fund it borrows from
can be refreshed.

**Do not re-derive the four US routes from the old comments in `etf_sources.py`'s history.**
They said SOXQ/GRID/QTUM were unreachable, and each was wrong in a different way: Invesco's SPA
hides a keyless API, First Trust's "tickers but no ISINs" missed the CUSIP column beside them,
and Defiance's table is on a different path. What the old note got right by accident is that
those identifiers do not fold on their own — see below.

**Fetching is split from parsing, and that is not tidiness.** `app/cli/fetch_etf_baskets.py`
writes response bodies to disk; `app/cli/import_etf_basket.py` parses and stores them. It is the
same relationship `ingest_flex_xml.py` has to a browser download, it makes every scraper's
failure mode a committable fixture, and **it is the only way to test the lying-content-type trap
at all** — a fetcher that parsed inline would have nothing to hand a test. The import path works
identically on a file downloaded by hand, which is the whole route for VWCE and the fallback for
any adapter whose route breaks.

### The identifier column that is not all CUSIPs

Three of the four US feeds publish a nine-character identifier where the others publish an ISIN,
and treating that column as CUSIPs is a **silent fabrication**, which is why
`app/services/security_identifiers.py` exists. Measured 2026-08-16:

- **77 of GRID's 128 rows are CINS**, not CUSIPs — the same numbering space extended to foreign
  issuers, marked by a *leading letter*. Its three largest holdings are all of them
  (`G29183103` Eaton, `F86921107` Schneider, `G51502105` Johnson Controls). `US` + a CINS is a
  **check-digit-valid ISIN belonging to nothing**, so a bulk prefix would invent an identifier
  for 60% of the fund and every one would look right.
- **20 of QTUM's 89 rows are SEDOLs** (`B056381`, `6640400`, `BZ1DZ96`) — seven characters, no
  vowels, their own check digit — sitting in a column headed "CUSIP".
- **Digit-leading is still not unambiguously US**: `82509L107` yields valid `US` and `CA` forms
  and only one exists. `derive_north_american_isin` names the assumption in its own signature.

So `identifier_kind()` classifies by *shape and check digit*, the ISIN is derived only for a
plain CUSIP, and everything else is kept verbatim in `etf_holdings.constituent_identifier` for
OpenFIGI. The failure direction is deliberate: a company that cannot be identified stands alone
as its own row (an understatement) rather than merging into another's (a fabrication).

**Resolution writes a FIGI, not an ISIN, because OpenFIGI has no ISIN to give.** Its `/v3/mapping`
answer is entirely FIGIs, tickers and exchange codes — `ID_CINS G29183103` returns 104 venue rows
sharing shareClassFIGI `BBG001S5QZ45`, which is exactly what `ID_ISIN IE00B8KQN827` returns, so
Eaton folds across GRID and MSCI World on the FIGI alone. `IdentityMember` already unions on
`share_class_figi`, so `etf_holdings.constituent_share_class_figi` needs no new grouping logic.
**And the idType matters more than it looks: asked as `ID_CUSIP`, that same CINS returns zero
rows** — no error, just nothing — so `OPENFIGI_ID_TYPES` maps CINS→`ID_CINS` and SEDOL→`ID_SEDOL`.
Verified against the live API before it was written, which is the only way to learn it.

**Everything the parsers refuse, they refuse whole.** `import_prices.py`'s rule, and here the
stakes are higher: a partial parse *succeeds*, replaces a real 1,338-row basket with plausible
rows, and every figure shrinks silently. On top of that `replace_basket` refuses a basket whose
row count collapses below half the stored one, or whose as-of moves backwards, keeping what is
already there. Both overridable with `--force` for a genuine index reconstitution.

**Ten things real files taught us, all now pinned by tests.** The first four came off the
European feeds, the rest off the four US ones:

- **A negative weight is refused only on an *invested* row.** EMIM publishes five negative cash
  lines (THB −0.01, TWD −0.01, CNH −0.01, HKD −0.02, KRW −0.10) — ordinary overdrawn balances.
  Refusing 4,042 rows over −0.10% of cash trades a whole fund's look-through for nothing. A
  negative *security* weight is the real hazard and still refuses. An issuer that states no class
  at all still refuses any negative, since there is no way to tell the two apart.
- **A nameless row falls back to its identifier.** XNAS ships `IE00BYQNZ507` with an empty name at
  0.008% of the fund. Nameless *and* unidentifiable still refuses.
- **Xtrackers' cash and futures rows are read off its identifier convention** (`_CURRENCYUSD`,
  `___ADI34XYM5`), because the export has no class column — without which XNAS produced company
  rows called *US DOLLAR* and *NASDAQ 100 E-MINI SEP26*. Only the negatives are derived; a real
  holding is left unclassified rather than asserted to be equity, which would be wrong for a bond
  fund. This is why `counts_as_invested` lets a **stated** class decide even when the issuer
  publishes no column.
- **`fund_residual_eur` is the rounded remainder, not its own rounded sum.** Five independently
  rounded buckets summed to a cent more than the rounded total on the real book — a partition
  that misses by a cent still misses. The residual carries the correction because "whatever is
  left" is its definition. Note the residual is mostly *rounding*, not cash, for a broad fund —
  and the counts say so rather than being inferred. Measured on production: Vanguard publishes
  weights to 2dp, the smallest non-zero weight in VT's file is `0.01`, and **8,007 of its
  10,032 rows are published at exactly 0.00%**, which is the whole of the 8.14% its weights fall
  short by. (EMIM is the same shape at 2,279 of 4,042.) Not renormalised — that would invent
  the attribution — and `unweighted_constituents` rides on each fund row so an 8% shortfall
  cannot read as an uninvested cash balance, which is the plausible wrong answer here.
- **Invesco HTML-escapes its names.** `Invesco Government &amp; Agency Portfolio` is the only
  place in seven feeds that happens, and an unescaped name reaches the screen.
- **`Money Market Fund, Taxable` is deliberately absent from `INVESCO_ASSET_CLASSES`.** An
  unmapped value passes through verbatim so `counts_as_invested`'s `"fund"` marker catches it;
  coercing unmapped values to `Equity` would make AGPXX a top-50 *company*.
- **First Trust's `Classification` column looks like an asset class and holds an industry**
  (`Diversified Industrials`, `Electrical Components`). Its nine currency lines are marked only
  by a `$`-prefixed ticker — the Xtrackers convention again, hence `asset_class_available=False`
  with the negatives still derived. The industry itself is stored raw and normalises to
  `Unknown`, which is correct: BlackRock classifies those companies anyway.
- **Do not date a basket from the first date-shaped string on the page.** QTUM's page carries
  five, including a bond maturity inside a holding's own name (`... Obligations Fund
  12/01/2031`). An unanchored search happened to pick a `17/02/2022` and refuse — the lucky
  outcome; the maturity would have parsed cleanly and dated the basket to 2031. `_us_date_after`
  takes an anchor phrase.
- **A declared count is worth checking wherever an issuer publishes one.** Invesco's
  `totalNumberOfHoldings` is the same guard as Vanguard's `size` — a truncated response whose
  weights still sum plausibly is the shape that gets through everything else.
- **And the pages of one paginated read must agree with each other about it.** Vanguard serves
  from a cluster whose nodes can hold different snapshots, so VT's 21-request walk came back
  **13 pages saying 10,055 holdings and 8 saying 10,032** — twice in a row, on 2026-08-17. The
  damage is wildly out of proportion to those 23: ~8,000 of VT's rows carry a **0.00% weight**
  and therefore no stable sort order between snapshots, so every page boundary crossing one
  duplicated a chunk and dropped another. Measured: 10,032 rows carrying **9,114 distinct
  holdings**, against 10,025 in the stored basket — ~900 companies would have vanished from a
  line that is ~11% of the book once VWCE's proxy is counted, with the weights still summing to
  a plausible 91.60%. It is refused as its own named fault, because the count check caught this
  only by the luck of the two totals differing, and "a page is missing" sends the operator
  hunting something that was never missing. **Pagination cannot be avoided** — `count` above 500
  returns a non-JSON body, not a capped page — and the retry is deliberately manual, since a
  cluster that stays split would spend 21 requests an attempt forever while the previous basket
  is kept anyway. Two snapshots with equal totals and different membership would still get
  through; that is recorded rather than guarded, because the legitimate duplicate rate (7 rows
  in 10,032, dual listings like BAM and BEPC) is measured and the torn one was 918, but one
  observation is not enough to calibrate a threshold on.

`app/cli/resolve_identities.py` resolves identity: a CLI rather than a route, following the
precedent that there is no upload endpoint for Flex XML and no route for price import. Being a
separate process it needs no gate — and if it ever becomes a route it must **not** use
`SYNC_PIPELINE`, because entering that gate bumps the shared last-start clock every other route's
cooldown reads, so a look-through refresh would 429 a real IBKR sync. Held ISINs are resolved
unconditionally (~25); `--constituents` adds the head of the constituent ranking, down to
`IDENTITY_COVERAGE_TARGET_PCT` (99.5) of cumulative look-through value and capped at
`IDENTITY_MAX_ISINS`, plus the CINS/SEDOL pass above. **The obvious form of the ranking rule is
circular** — it wants a company's value to decide whether to resolve its identity, and identity is
what builds companies — so it ranks by *raw constituent ISIN* first, which needs no identity at
all. The threshold is a share rather than an amount because the base currency is user-switchable
and a fixed floor would change the resolved set when a display toggle is flipped.

**`OPENFIGI_API_KEY` is free, optional, and the precondition for the cap being worth raising.**
It takes OpenFIGI from 10 mapping jobs per request at 25 requests/minute to 100 at 250 — roughly
250 identifiers a minute to 25,000. Empty by default, so the feature keeps working without it,
and the CLI says when it is missing. **GLEIF is what actually bounds a run**, though: it has no
batch form for an ISIN filter, so it costs ~1.1s per ISIN — about 45 minutes at
`IDENTITY_MAX_ISINS` (2500) against a couple of minutes for OpenFIGI's share. Hence the runtime
estimate printed before the first request, and `--limit`: a definitive answer is cached either
way, so three bounded runs come to the same thing as one long one.

Tests: `test_company_identity.py`, `test_lookthrough_partition.py`, `test_etf_source_registry.py`,
`test_etf_basket_import.py` and `test_us_issuer_adapters.py` (the poison fixtures),
`test_security_identifiers.py`, `test_constituent_identifier_resolution.py`, plus the look-through
case in `test_api_smoke.py` and `LookThroughTab.test.tsx`.

---
