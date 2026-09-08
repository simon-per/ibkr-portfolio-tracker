# CLAUDE.md — size, load-frequency and staleness analysis

Measurement only. Target: `CLAUDE.md` at commit `69c8eb8`, 2026-08-26.

## Method

| quantity | how obtained |
|---|---|
| bytes | `wc -c CLAUDE.md` → **251,708** |
| unicode chars (CRLF as 2) | Python `len(raw.decode('utf-8'))` → **250,221** |
| chars, LF-normalised | Python text-mode read → **247,158** (the unit used throughout) |
| lines | `wc -l` → **3,064** (file is CRLF; `file` confirms) |
| tokens, measured | `tiktoken` `o200k_base` over the whole file → **61,721** (4.004 chars/token) |
| tokens, as this session actually loaded it | `/context` readout, "Memory files" → **~92,000** |
| est. Claude tokens per section | o200k count × **1.4906** (= 92,000 / 61,721): per-section o200k density rescaled to the harness's own total |

The two token figures disagree by 1.49×. The o200k number is a precise *relative* density measure per section; the 92k is the only ground truth for how the file loads. Per-section Claude estimates are the former calibrated to the latter, so they sum to ~92k by construction. Nothing here assumes 4 chars/token — the measured whole-file o200k ratio is 4.00, but per-section ratios range 3.68–4.36.

---

## 1. The truncation question

### Where 150,000 lands

| unit counted | lands at line | inside |
|---|---|---|
| 150,000th **byte** | 1910 | `## Look-through — company-level exposure` → `### Both identity providers are required…` |
| 150,000th **unicode char** | 1921 | same subsection |
| 150,000th **LF-normalised char** | 1938 | same `##`, in `### The partition, and why it is the only assertion that matters` |

All three land inside `## Look-through — company-level exposure` (lines 1860–2417). Obtained by Python
cumulative counts over the raw bytes and over the decoded text.

### Every heading at or after the mark (using the earliest, byte-based, line 1910)

| line | heading |
|---|---|
| 1892 | `### Both identity providers are required, and they fail in opposite directions` *(mark falls inside)* |
| 1929 | `### The partition, and why it is the only assertion that matters` |
| 1946 | `### Nine rules, each a wrong number the other way` |
| 1981 | `### The two charts, and why neither is a pie` |
| 2009 | `### Grouping the treemap by sector, and the one prohibition it narrows` |
| 2076 | `### One fundness predicate, keyed on ISIN` |
| 2107 | `### Baskets, and DBPG` |
| 2201 | `### Sources, and what still needs a hand download` |
| 2236 | `### A borrowed basket, and the three ways it could have become a silent one` |
| 2291 | `### The identifier column that is not all CUSIPs` |
| 2418 | `## Client-side analytics — risk, targets, currency` |
| 2448 | `### Risk metrics (portfolioKpis.ts)` |
| 2520 | `### Target allocation and drift (rebalance.ts)` |
| 2565 | `### Currency exposure (currencyExposure.ts)` |
| 2589 | `## The mobile layout — one description, two renderings` |
| 2662 | `## Deployment` |
| 2715 | `## Local development` |
| 2772 | `## Ticker mapping & currency` |
| 2808 | `### Managing mappings — app/cli/manage_mappings.py` |
| 2876 | `## Troubleshooting` |
| 2954 | `## Correctness sweep (2026-07-29)` |
| 2970 | `## Current state (2026-07-28)` |

That is **101,708 bytes / 40.4% of the file** after the mark.

### What the docs say

Fetched https://code.claude.com/docs/en/memory. Verbatim:

> "This limit applies only to `MEMORY.md`. **Claude Code loads a CLAUDE.md file of up to 4 MiB in full and
> skips a larger file.** Shorter files produce better adherence."

and, under *My CLAUDE.md is too large*:

> "Files over 200 lines consume more context and may reduce adherence. Claude Code skips a file over 4 MiB."

**Findings:**

1. The page states **no 150,000-character limit at all.** The only numeric limits it gives for CLAUDE.md are
   a **4 MiB** hard cutoff and a soft **200-line** target. The figure 150,000 appears nowhere on the page.
2. Behaviour at the 4 MiB limit is **skip the whole file**, not truncate. No partial-load or truncation mode
   for CLAUDE.md is documented.
3. This file is **251,708 bytes = 0.24 MiB**, ~4.3% of the 4 MiB cutoff.
4. The 200-line/25KB truncation the docs *do* describe ("everything past the limit is dropped on the next
   load") applies explicitly and only to auto-memory `MEMORY.md`.

So on the documented behaviour, **nothing after line 1910 has been absent from sessions** — the file loads whole. The premise that a 150k warning implies truncation is not supported by the docs. What the docs do support is the adherence claim: "Longer files consume more context and reduce adherence."

Two caveats stated rather than guessed: (a) the docs do not describe where a 150,000-character client-side warning originates, so its exact semantics are undocumented on that page; (b) this session's `/context` reports CLAUDE.md at 92k tokens, consistent with a full load of 247k chars but not by itself proof of completeness. No claim here rests on the fact that the Read tool can see the file.

---

## 2. Size breakdown

### `##` sections, descending

Char counts from a Python pass assigning every line to the `##` heading above it; token counts from
`tiktoken` over the same line ranges.

| # | section | line | chars | % | o200k tok | est. Claude tok |
|---|---|---|---|---|---|---|
| 1 | Sync schedule (Europe/Berlin) | 1235 | 43,180 | 17.5% | 10,993 | 16,386 |
| 2 | Look-through — company-level exposure | 1860 | 42,271 | 17.1% | 10,713 | 15,969 |
| 3 | Dividends — history and forecast | 799 | 22,894 | 9.3% | 5,620 | 8,377 |
| 4 | Troubleshooting | 2876 | 20,332 | 8.2% | 4,955 | 7,386 |
| 5 | Reconciliation & realized P&L | 545 | 15,248 | 6.2% | 3,712 | 5,533 |
| 6 | IBKR Flex Query integration | 273 | 14,500 | 5.9% | 3,744 | 5,581 |
| 7 | Client-side analytics — risk, targets, currency | 2418 | 12,506 | 5.1% | 3,082 | 4,594 |
| 8 | The dominant failure mode: two implementations of one job, drifting | 158 | 12,087 | 4.9% | 2,841 | 4,235 |
| 9 | Contributions — money in per month | 1093 | 10,052 | 4.1% | 2,432 | 3,625 |
| 10 | Current state (2026-07-28) | 2970 | 7,483 | 3.0% | 1,900 | 2,832 |
| 11 | Ticker mapping & currency | 2772 | 7,367 | 3.0% | 1,844 | 2,749 |
| 12 | ⚠️ Two rules that must never be broken | 19 | 7,332 | 3.0% | 1,873 | 2,792 |
| 13 | The mobile layout — one description, two renderings | 2589 | 5,417 | 2.2% | 1,326 | 1,977 |
| 14 | Database schema | 485 | 4,527 | 1.8% | 1,229 | 1,832 |
| 15 | Tax report (Swiss framing) | 739 | 4,424 | 1.8% | 1,102 | 1,643 |
| 16 | Activity ledger | 1802 | 4,101 | 1.7% | 1,001 | 1,492 |
| 17 | Deployment | 2662 | 3,413 | 1.4% | 906 | 1,350 |
| 18 | Local development | 2715 | 3,054 | 1.2% | 761 | 1,134 |
| 19 | Tech stack | 235 | 2,733 | 1.1% | 704 | 1,049 |
| 20 | Keeping STATUS.md current | 128 | 1,909 | 0.8% | 438 | 653 |
| 21 | Correctness sweep (2026-07-29) | 2954 | 1,307 | 0.5% | 308 | 459 |
| 22 | (preamble, lines 1–18) | 1 | 1,021 | 0.4% | 237 | 353 |

Top 2 sections = **34.6%** of the file. Top 5 = **58.3%**.

### `###` subsections within `##` sections over 15k chars

| parent | line | subsection | chars | est. Claude tok |
|---|---|---|---|---|
| Sync schedule | 1374 | The 18:00 Berlin slot — chosen against the evidence… | **33,538** | 12,418 |
| Sync schedule | 1235 | (intro, before first ###) | 9,642 | 3,968 |
| Look-through | 2291 | The identifier column that is not all CUSIPs | 10,153 | 3,880 |
| Look-through | 2107 | Baskets, and DBPG | 7,312 | 2,735 |
| Look-through | 2009 | Grouping the treemap by sector… | 5,014 | 1,841 |
| Look-through | 2236 | A borrowed basket, and the three ways… | 4,006 | 1,461 |
| Look-through | 2201 | Sources, and what still needs a hand download | 3,199 | 1,291 |
| Look-through | 1892 | Both identity providers are required… | 2,745 | 1,125 |
| Look-through | 1946 | Nine rules, each a wrong number the other way | 2,742 | 1,015 |
| Look-through | 2076 | One fundness predicate, keyed on ISIN | 2,397 | 881 |
| Look-through | 1981 | The two charts, and why neither is a pie | 1,931 | 686 |
| Look-through | 1860 | (intro) | 1,010 | 428 |
| Look-through | 1878 | Grouping is a union, never a precedence chain | 892 | 325 |
| Look-through | 1929 | The partition, and why it is the only assertion… | 870 | 301 |
| Dividends | 799 | (intro) | 6,349 | 2,363 |
| Dividends | 982 | The forward yield — the portfolio's dividend rate | 5,572 | 2,050 |
| Dividends | 912 | The forecast — four rules that were each a bug first | 5,462 | 1,962 |
| Dividends | 876 | A wrong mapping poisons dividends too… | 2,809 | 1,040 |
| Dividends | 1053 | Growth — MoM / YoY, and the five ways it lies | 2,702 | 963 |
| Troubleshooting | 2876 | *(no `###`; one 20,306-char table)* | 20,332 | 7,386 |
| Reconciliation | 545 | *(no `###`; 194 lines of flat prose)* | 15,248 | 5,533 |

**Structural note**, from `awk` for any `#`-heading between lines 1235 and 1802: `## Sync schedule` contains exactly one `###`, at line 1374, and it absorbs **33,538 chars (77.7% of the section)**. Its title names the 18:00 Berlin slot; a scan of the 30 bold lead-ins inside it shows the content spans five unrelated subjects:

| lines | actual subject | approx chars |
|---|---|---|
| 1374–1432 | the 18:00 slot + `find_flex_generation_gap` | 4,500 |
| 1433–1510 | full_sync decoupling, `1001` history, Flex period | 6,300 |
| 1511–1566 | `single_flight` gating, benchmark gate | 4,600 |
| 1567–1718 | the unpriced-holdings / zero-for-unknown family | 12,400 |
| 1719–1775 | `find_stale_ibkr_sync`, persistent job store | 4,400 |
| 1776–1801 | `app/auth.py`, `rate_limit.py`, `observability.py` | 2,300 |

---

## 3. Load-frequency classification

Classes assigned by reading each section's opening and asking what task must be in hand for it to matter. Char sums from the same Python pass as §2.

| section | class | scope |
|---|---|---|
| preamble (1–18) | EVERY | base currency, STATUS.md pointer, live URL, public-repo warning |
| ⚠️ Two rules that must never be broken | EVERY | Yahoo + Flex prohibitions |
| Keeping STATUS.md current | EVERY | process obligation |
| The dominant failure mode | EVERY | the file's governing principle |
| Tech stack | EVERY | |
| Deployment | EVERY | push-to-main behaviour, `/health` |
| Local development | EVERY | `SCHEDULER_ENABLED=false` trap, test commands |
| Database schema | DIR | `backend/` |
| Client-side analytics | DIR | `frontend/src/lib/` |
| The mobile layout | DIR | `frontend/src/components/` |
| IBKR Flex Query integration | SUBSYS | `ibkr_service` / Flex ingest |
| Reconciliation & realized P&L | SUBSYS | `sync_helper` + `portfolio_service` |
| Tax report | SUBSYS | `tax_service` |
| Dividends | SUBSYS | `dividend_service` |
| Contributions | SUBSYS | `get_contributions` / `cash_flows` |
| Sync schedule | SUBSYS | `scheduler_service` + API hardening + unpriced family (see §2 note — three subsystems) |
| Activity ledger | SUBSYS | `activity_service` |
| Look-through | SUBSYS | `lookthrough_service` / ETF baskets |
| Ticker mapping & currency | SUBSYS | `market_data_service` + `currency_service` |
| Troubleshooting | LOOKUP | symptom-indexed table |
| Correctness sweep (2026-07-29) | DATED | |
| Current state (2026-07-28) | DATED | |

| class | sections | chars | % of file | est. Claude tok |
|---|---|---|---|---|
| **EVERY** | 7 | **31,549** | **12.8%** | ~11,567 |
| DIR | 3 | 22,450 | 9.1% | ~8,402 |
| SUBSYS | 9 | 164,037 | 66.4% | ~61,354 |
| LOOKUP | 1 | 20,332 | 8.2% | ~7,386 |
| DATED | 2 | 8,790 | 3.6% | ~3,291 |
| total | 22 | 247,158 | 100% | ~92,000 |

The EVERY total, **31,549 chars / ~11.6k tokens**, is the realistic floor. Two qualifications:

- 8,157 chars (67%) of `## The dominant failure mode` are its instance **table** at line 164 — a catalogue of
  past incidents, not the principle. The principle statement itself is ~3,930 chars.
- `## Deployment` and `## Local development` are borderline: their first paragraphs are EVERY, the remainder
  is `ops/` and VPS detail.

---

## 4. Redundancy

### Method

The file was split into 468 blank-line-separated paragraphs. Five paragraphs over 2,500 chars are tables or long bullet lists that match many patterns at once and would dominate any per-paragraph metric, so they are reported separately and excluded from the principle table:

| line | chars | what |
|---|---|---|
| 2878 | 20,306 | the Troubleshooting table |
| 164 | 8,157 | the "how it diverged" table |
| 2330 | 5,158 | "Ten things real files taught us" |
| 487 | 3,130 | the schema bullet list |
| 1948 | 2,690 | "Nine rules" |
| | **39,441** | **16.0% of the file is five blocks** |

The remaining 463 paragraphs (207,248 chars) were matched against a vocabulary regex per principle. A
paragraph can match more than one.

### Recurring principles

| principle | paras | chars | % of non-table text |
|---|---|---|---|
| P10 dated incident narrative ("was a bug first") | 47 | 32,294 | 15.6% |
| P2 one job, two implementations → extract | 31 | 20,763 | 10.0% |
| P9 bound every retry; never re-ask forever | 22 | 14,321 | 6.9% |
| P1 an unknown must be absent, never a stand-in value | 14 | 13,383 | 6.5% |
| P7 a caveat behind a hover/collapse does not exist | 14 | 10,670 | 5.1% |
| P8 latch the fault, surface it in `warnings[]` | 14 | 9,124 | 4.4% |
| P3 exclude the unvaluable from both sides **and name it** | 8 | 5,607 | 2.7% |
| P6 never renormalise; report the shortfall | 6 | 5,277 | 2.5% |
| P4 all-or-nothing refusal (never partially apply) | 3 | 1,759 | 0.8% |
| P5 whitelist, never blacklist | 2 | 842 | 0.4% |
| **union** | **132** | **85,980** | **41.5%** |

Line starts, for locating the instances:

- **P1** 777, 780, 1003, 1069, 1654, 1836, 2187, 2265, 2455, 2477, 2486, 2496, 2501, 2531
- **P2** 158, 192, 208, 222, 429, 437, 463, 934, 1037, 1058, 1224, 1258, 1273, 1331, 1376, 1548, 1826, 1888, 2134, 2150, 2206, 2244, 2520, 2522, 2531, 2540, 2636, 2655, 2819, 2856, 3014
- **P3** 463, 600, 777, 1003, 1591, 1609, 1674, 2554
- **P6** 816, 1936, 1995, 2244, 2307, 2531
- **P7** 458, 833, 984, 1041, 1063, 1171, 1609, 1617, 1636, 1646, 1654, 1704, 2477, 2501
- **P8** 446, 450, 458, 472, 727, 758, 791, 1182, 1304, 1654, 1725, 2187, 2265, 2842
- **P9** 69, 86, 90, 108, 121, 424, 477, 678, 698, 727, 1111, 1451, 1530, 1539, 1553, 1558, 1743, 1901, 2389, 2864, 3028
- **P10** 11, 61, 69, 297, 301, 365, 450, 552, 583, 600, 689, 758, 807, 833, 862, 878, 884, 912, 1021, 1150, 1243, 1248, 1287, 1312, 1326, 1337, 1376, 1380, 1421, 1433, 1558, 1617, 1816, 1826, 1894, 2088, 2177, 2244, 2433, 2465, 2501, 2540, 2617, 2819, 3048, 3052, 3056

### Principle statement vs instance mass

| principle | where stated | statement chars | instance chars | ratio |
|---|---|---|---|---|
| P2 (duplication drifts) | lines 158–163 + 214–234 | ~3,930 | 20,763 (31 paras) + 8,157 (line-164 table) = **28,920** | **1 : 7.4** |
| P1 (absent, not a stand-in) | lines 2496–2500, "the lens worth reusing" | ~640 | **13,383** (14 paras) | **1 : 20.9** |

Across all ten principles: statements ~**6,900 chars**, instances **85,980 chars** — a **1 : 12.5** ratio.
That instance mass is **34.8% of the whole file**.

### The rhetorical unit

`grep` for paragraphs beginning `**`: **148 "\*\*Bold claim.\*\* explanation" units, 85,641 chars (34.7% of
the file)**. 16 of them carry a date in their first line.

### Same rule restated under different headings

| rule | stated at | restated at |
|---|---|---|
| unpriced holdings excluded and counted | `## Sync schedule` 1567–1718 (~12,400 ch) | `## Client-side analytics` 2455–2564; `## Look-through` 1946–1980; `## Troubleshooting` ×8 rows |
| era splice (yfinance ex-date vs IBKR pay-date) | `## Dividends` 838–875 | `## Tax report` 745–757; `## Activity ledger` 1816–1841; `## Reconciliation` (XIRR inflows) |
| `_to_eur` returns `None` on FX failure | `## The dominant failure mode` table rows 3 and 11 | `## Tax report` 777–784; `## Contributions` currency block |
| `PROVISIONAL_PRICE_DAYS` / re-fetch a recent close | `## Reconciliation` 689–712 | `## Sync schedule` 1287–1300; `## Troubleshooting` ×1 row |
| "an unpriced fund's absence looks like completeness" | `## Look-through` 1976–1980 | `## Sync schedule` 1674–1699; `## Client-side analytics` 2531–2539 |
| a caveat inside a collapsible is absent | `## Sync schedule` 1609–1626 | `## Look-through` 1948; `## Dividends` 1041 |
| ADR/ordinary identity folding | `## Look-through` 1892–1928 | `### The identifier column…` 2291–2417; `## Troubleshooting` ×1 row |

### Self-contradictions found

| # | claim | contradicted by | verdict |
|---|---|---|---|
| 1 | line 2139: "**the five daily feeds** (Xtrackers, iShares, Invesco, First Trust, Defiance, VanEck)" | the same sentence lists **six**; line 2936 says "**the six** that republish daily" with the identical list; `ADAPTER_STALE_DAYS` (`lookthrough_service.py:78`) has **six** adapters at 7 days | **wrong** — "five" should be six |
| 2 | line 1283: "**The two IBKR-only jobs** exist because…" | `scheduler_service.py:201` `IBKR_ONLY_HOURS = (0,)` registers **one** (`ibkr_retry_1`); the section's own table at 1239 lists only 00:00 | **stale** — leftover from the 00:00+06:00 era |
| 3 | line 2230: "**All 14 funds** decompose" | `FUND_SOURCES` (`etf_sources.py`) has **15** entries; `ETF_ALLOCATIONS` has **15** | **off by one** |
| 4 | line 1867: "48% of this account sits in **twelve ETFs**" | 15 declared, of which VT and VOO are donor-only per lines 2230/2288 → 13 held | **not reconcilable** with 14 or 15 |
| 5 | line 2626: "**sixteen KPI cards** pass `text-sm` to `CardTitle`" (present tense) | `grep -rho 'CardTitle[^>]*text-sm' frontend/src` → **2**; `<KpiCard` appears 40× | **stale** — describes the pre-extraction state as current |
| 6 | line 2591: "**Thirteen tables** times two renderings" | 11 non-test files render `<DataTable>`; 26 occurrences incl. tests | **approximate / drifted** |
| 7 | lines 301–310 declare "no constant in this repo is allowed to assert" the Flex period and "treat any number written down here as hearsay" | the same paragraph then asserts "The live period is `Last 30 Calendar Days`", repeated at 1402, 1422, 1496 | **self-referential tension**, not a factual error — all 14 window mentions checked, all consistent at 30; the known prior failure (N=3 asserted as fact) is corrected everywhere |

---

## 5. Staleness signals

### Constants with a value stated in prose — verified against the repo

`rg` for `^\s*NAME\s*[:=]` across `backend/ frontend/ e2e/ ops/`.

| constant | doc value | line | repo | match |
|---|---|---|---|---|
| `FALLBACK_MAX_AGE_DAYS` | 7 | 2849 | `currency_service.py:54` = 7 | ✅ |
| `FLEX_GENERATION_GAP_WARN_DAYS_FLOOR` | 2 | 1429 | `scheduler_service.py:128` = 2 | ✅ |
| `HOLIDAY_GRACE_DAYS` | 30 | 682 | `market_price_repository.py:66` = 30 | ✅ |
| `IBKR_SYNC_STALE_DAYS` | 7 | 1722 | `scheduler_service.py:111` = 7 | ✅ |
| `IDENTITY_COVERAGE_TARGET_PCT` | 99.5 | 2395 | `lookthrough_service.py:105` = `Decimal("99.5")` | ✅ |
| `IDENTITY_MAX_ISINS` | 2500 | 2407 | `lookthrough_service.py:116` = 2500 | ✅ |
| `MIN_BASKET_COVERAGE_PCT` | 80 | 1960 | `lookthrough_service.py:64` = `Decimal("80")` | ✅ |
| `MISFIRE_GRACE_SECONDS` | 1800 | 1747 | `scheduler_service.py:160` = `30 * 60` | ✅ |
| `PRICE_LOOKBACK_DAYS` | 14 | 1582 | `portfolio_service.py:34` = 14 | ✅ |
| `PROVISIONAL_PRICE_DAYS` | 3 | 695 | `market_price_repository.py:89` = 3 | ✅ |
| `STALE_PRICE_DAYS` | 5 | 1570 | `scheduler_service.py:88` = 5 | ✅ |
| `UPSTREAM_RETRY_COOLDOWN_SECONDS` | 300 | 1542 | `benchmark_service.py:42` = 300 | ✅ |
| `EX_TO_PAY_MAX_LAG_DAYS` | 30 | 838 | `dividend_service.py:56` = 30 | ✅ |
| `COST_CONSERVED_RATIO` | 0.99 implied | 560 | `sync_helper.py:478` = `Decimal("0.99")` | ✅ |
| `MAX_RANGE_DAYS` | `365 * 5` | 2646 | `dateRanges.ts:32` = `365 * 5` | ✅ |
| `PRICE_FETCH_BUFFER_DAYS` | named only | 719 | `market_data_service.py:26` = 5 | ✅ exists |
| `PRE_OWNERSHIP_HISTORY_YEARS` | named only | 869 | `dividend_service.py:33` = 3 | ✅ exists |
| `UPPERCASE_ADVANCE_PX` | named only | 2072 | `LookThroughCharts.tsx:150` = 7.6 | ✅ exists |
| `RATE_LIMIT_PER_MINUTE` | named only | 1790 | `rate_limit.py:21` | ✅ exists |
| `SECTOR_COLORS` | named, past tense ("failed validation outright") | 2054 | **not in repo** | ✅ correctly described as replaced by `lib/sectorColors.ts` |

Schedule constants: `FULL_SYNC_HOUR = 18`, `MARKET_DATA_HOURS = (8, 11, 13, 15, 20, 22)`, `IBKR_ONLY_HOURS = (0,)` (`scheduler_service.py:201–210`). The table at line 1239 matches on all three; "seven Yahoo hours — 8, 11, 13, 15, 18, 20, 22" (line 1250) reconciles as the six market-data hours plus full_sync's 18. Only the "two IBKR-only jobs" sentence at 1283 disagrees (§4 #2).

`ADAPTER_STALE_DAYS` (`lookthrough_service.py:78`): dws/blackrock/invesco/first_trust/defiance/vaneck = 7,
vanguard_us = 75, manual = 45 — matches the doc's "7 / 75 / 45" at line 2936 exactly.

### File and symbol references

Extracted 137 backtick-quoted paths ending in a source extension; resolved each against the tree.

| result | count |
|---|---|
| resolve to an existing file | **136** |
| not found | **1** — `/root/ibkr-backups/sbi-poisoned-2026-07-27.json` (line 3029; a VPS path, not expected in-repo) |
| resolve under a different root than written | 1 — `/root/auto-deploy.sh` (line 2664) → `ops/auto-deploy.sh` |

**No dead test-file, module or CLI reference.** Every `tests/test_*.py`, `app/cli/*.py`, `lib/*.ts` and
`ui/*.tsx` named in the file exists.

### Counted figures

| claim | line | measured | verdict |
|---|---|---|---|
| "1195 backend … tests as of 2026-08-17" | 2748 | `pytest --collect-only -q` → **1272 collected** | stale **+77** |
| "489 frontend" | 2748 | `npx vitest run` → **505 passed / 37 files** | stale **+16** |
| "eight warm benchmarks" / "the 8 keys in `BENCHMARKS`" | 1301, 1531 | `BENCHMARKS` → **8** | ✅ |
| "eight non-default tabs are `React.lazy`" | 254 | `Dashboard.tsx` → **8** lazy calls | ✅ |
| "Seven adapters" | 2203 | 8 distinct `adapter=` values incl. `manual` → **7** real fetchers | ✅ |
| "`ISSUER_OVERRIDES` … currently one entry" | 1922 | **1** | ✅ |
| "Two entries: VWCE borrows VT's and DBPG borrows VOO's" | 2237 | 2 `basket_proxy_isin=` | ✅ |
| "Three tables deliberately stay tables" | 2606 | 3 non-`ui/` files with a raw `<table>` | ✅ |
| "`e2e/errors.mjs` … It is `>= 10` now" | 2445 | `errors.mjs:53` `hits >= 10` | ✅ |
| "the six 7-day `market_data_only` slots" | 1442 | `MARKET_DATA_HOURS` len 6 | ✅ |
| "9th tab" (Look-through) | 1861 | 8 lazy + 1 default = 9 | ✅ |
| "All 14 funds decompose" | 2230 | 15 | ✗ (§4 #3) |
| "twelve ETFs" | 1867 | 13 held / 15 declared | ✗ (§4 #4) |
| "the five daily feeds" | 2139 | 6 | ✗ (§4 #1) |
| "sixteen KPI cards pass `text-sm`" | 2626 | 2 | ✗ (§4 #5) |
| "Thirteen tables" | 2591 | 11 files | ✗ (§4 #6) |
| "The two IBKR-only jobs" | 1283 | 1 | ✗ (§4 #2) |

### Dates

94 ISO dates, from `grep -o '20[0-9][0-9]-[0-9][0-9]-[0-9][0-9]' | sort | uniq -c`. Most-cited:
2026-08-17 (15), 2026-08-05 (14), 2026-07-30 (10), 2026-07-31 (7), 2026-08-04 (6), 2026-08-08 (5),
2026-07-27 (5), 2026-08-07 (4), 2026-08-24 (3). Newest date in the file: **2026-08-24**; today is 2026-08-26.
All are backward-looking incident stamps; none is a future commitment.

### Production-snapshot figures (not checkable from the repo — no DB in-tree)

| claim | line |
|---|---|
| "40 securities, 36 open positions, 975 open tax lots, 67 trades, 62,178.99 CHF" | 2972 |
| "979 lots", "71 YTD trades", "`coverage_from` still 2026-01-09" | 1501 |
| "47 cash flows = 25 deposits + 22 in-kind transfers" | 2985 |
| "26 IBKR dividend payments"; "1350 zero rows removed" | 3042, 866 |
| "SBI … 4.79 CAD / 276.83 CHF, 501 cached prices" | 3027 |
| "Reconciled against IBKR to 0.12%"; "282 YTD trades = 218 CASH + 64 STK" | 3048, 3052 |
| "VT: 10,032 rows / 9,114 distinct / 8,007 at 0.00%" | 2373, 2352 |
| "48% of this account sits in twelve ETFs" | 1867 |
| "MBGL … `unpriced_holdings = 1` on 166 consecutive days"; "YTD of +3.1%"; "+23.5%" | 605–615 |
| "64,944 / 56,009 / −100%" timeline table | 1580 |
| "97 of 103 ISINs classified by BlackRock, 34 DWS, 27 Yahoo" | 2027 |
| "77 of GRID's 128 rows are CINS"; "20 of QTUM's 89 are SEDOLs" | 2299, 2303 |

Line 3059 states these are described rather than published because the repo is public and a pasted total
goes stale silently, and directs the reader to check the API or DB. The two `DATED` sections
(`## Correctness sweep (2026-07-29)`, `## Current state (2026-07-28)`) total 8,790 chars / 3.6% and carry
headline dates ~4 weeks before today.
