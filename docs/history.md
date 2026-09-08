# Dated snapshots — the 2026-07 correctness sweep and the 2026-07-28 state

> Moved verbatim from `CLAUDE.md` on 2026-09-08, when that file became a short index so a
> session no longer loads ~92k tokens of documentation to start. **This file is
> authoritative for its subsystem**: every invariant here was a bug first. Keep it accurate
> the way CLAUDE.md was kept — update it in the same change that moves the code it describes.

## Correctness sweep (2026-07-29)

A three-part audit (valuation core / API surface / frontend) produced 16 fixes, all shipped and
deployed overnight; the test suite went 190 → 241. The ones that changed **stated behaviour** are
documented in place above — token redaction and the single-flight gate under *Sync schedule*, the
close-date convention, disposal inflows and the swept timeline under *Reconciliation*, the era splice
and forecasts under *Dividends*, the holiday rule under *split invalidation*. Also fixed and worth
knowing: the FX carry-forward is now bounded at **30 days** (it had no bound, so a months-stale rate
could be stamped onto a new lot's persisted `cost_basis_eur` — past the bound it raises and the lot is
skipped *with* a warning, which self-heals); the price-currency map picks the **newest** row
deterministically and warns on a mixed history instead of applying an arbitrary row to the whole series;
two composite indices were added (`taxlots(security_id, is_open)`,
`exchange_rates(from_currency, to_currency, date)`); and the UI now renders sync **warnings** (they ride
on *successful* runs and were structurally unreachable before — the SBI silent-failure class) and shows
an explicit error state instead of "No portfolio data — sync to get started" when the backend 500s.

## Current state (2026-07-28)

**40 securities, 36 open positions, 975 open tax lots, 67 trades, 62,178.99 CHF.** The 27 Jul
statement was ingested **offline** from a browser download (`ibkr_manual_xml`, 04:16 UTC) rather than
waiting on the Flex API, because three consecutive IBKR jobs had returned a plain `1001`. That is the
escape hatch working as designed: no token spend, no `1025` exposure, and `full_sync` re-ingesting the
same statement afterwards is idempotent.

`taxlots_skipped: 0`, `prices_invalidated: 0`, no "unsupported currencies" warning, and
`find_stale_priced_securities()` returns empty — every held security has a current price.

**The external cash ledger is live on production.** The Flex Query now carries
Deposits & Withdrawals (at **Detail**) and the **Transfers** section (at **Transfer** level, not Lot —
Lot would emit a row per transferred lot and bury the cash leg in the list that has to be audited).
The statement ingested **47 cash flows = 25 deposits + 22 in-kind transfers**, 0 skipped, and
**0 reclassified** — correct, because the transfer carried no cash (see the contributions section).
No manual reclassification was needed, so the automatic guard has never had to fire on this account.
`manage_cash_flows list` shows all 22 transfer rows as *not* counted, which is the audit to run before
trusting any money-added figure.

It reached production through the **offline path** (`ibkr_manual_xml`), not the Flex API: three
consecutive IBKR-only jobs had returned a plain `1001`, so a browser download was ingested instead —
no token spend, no `1025` exposure, and the next `full_sync` re-ingests the same statement idempotently.

`deposits_from` lands a few days *before* the transfer date, so the ledger genuinely starts at the
account's first funding. The set includes one real withdrawal (negative amount, sign preserved) and one
CHF deposit against an otherwise-EUR ledger, which is what exercises the FX path end to end. Note IBKR
sends the **legacy** `type="Deposits/Withdrawals"` spelling, not `"Deposits & Withdrawals"` — ibflex maps
both to `CashAction.DEPOSITWITHDRAW`, so nothing special is needed, but don't "fix" the enum comparison
if that string looks wrong.

`coverage_from` = the ledger's **first row**, in the second week of January — *not* the statement period
start, which is 1 Jan because the query is YTD. The account's first deposit and its first execution land
on the same day, so that is genuinely the date IBKR becomes the whole picture; the clamp described in the
contributions section is what stops the pre-account days of January being claimed as covered and their
purchases dropped. The incoming transfer arrives *later* than that date, which is why the boundary is the
ledger start and not the transfer. All-time and 12M come out `spliced`; 6M and 3M run on
**deposits alone** and are already rotation-proof.
Where both sources overlap they agree to within **~12%** — two independent derivations (lot cost basis
vs. the cash ledger) landing that close is the best available evidence that the pre-ledger lot-based
figures were sound. `Σ monthly[].net_eur` matches the open-lot cost basis **to the cent in EUR**; in CHF
it lands a few francs off, which is the per-date FX projection on four closed lots and not an error —
see the identity check in the contributions section.

**That statement carried a large IBKR schema drift and needed no code change.** 20+ new attributes
(`figi`, `issuerCountryCode`, `serialNumber`, `weight`, `subCategory`, `exDate`, `dividendType`,
`origTransactionID`, `initialInvestment`, …) plus a `Trade.notes` value `RI` that ibflex can't convert
to its enum tuple. `_sanitize_flex_xml()` dropped all of them generically. This is the case the
sanitizer was written for — don't start patching attribute names.

**The two new positions both landed cleanly.** `2330@TWSE` (TSMC, 12 sh, TWD) and `SOXQ@NASDAQ`
(7.5 sh, USD), both bought 2026-07-27. They worked because the FX rates their lots are valued at were
already cached for that exact date (`reconcile_taxlots` uses `open_date`) — without the TWD row TSMC
would have been silently skipped. `2330/TWSE → 2330.TW` is pinned `manual`; **SOXQ deliberately has no
mapping** and resolved through the bare ticker, which is correct: `NASDAQ` gives an empty suffix, so
that *is* what tier 2 produces, and with no suffix `_get_yahoo_ticker_variations()` returns a single
candidate — the bare-symbol auto-save that poisoned SBI is never reached.

**SBI is fully repaired: 4.79 CAD / 276.83 CHF, 501 cached prices.** Its poisoned rows were deleted
(backup: `/root/ibkr-backups/sbi-poisoned-2026-07-27.json`), 20 days were imported from Client Portal
bars (`source='ibkr'`, `2026-06-29..07-27`) and Yahoo later filled the other 481 via the `manual`
`SBI/TSE → SBI.TO` mapping. The two windows don't overlap — the imported dates were never re-fetched,
exactly as `get_missing_dates()` implies. Yahoo's 27 Jul close for `SBI.TO` came back at **4.79 CAD**,
identical to IBKR's own bar, which independently confirms both the mapping and the import.

Dividends: 57 cash-transaction rows → 26 IBKR dividend payments, all with real withholding, running
from mid-February. 2026 reports `dividend_source='ibkr'` and `realized_source='trades'`; realized is
a small net loss over 4 closed lots. The tax report's `holdings_snapshot_total` matches the portfolio
summary to the cent, which is the shared-code guarantee holding.

Figures are described rather than published, as elsewhere in this file — the repo is public, and a
pasted total also goes stale silently: the ones that used to sit here were superseded when the manual
XML re-ingest upserted corrected amounts, and read as a discrepancy months later. **Check the numbers
against the API or the DB, never against this file.** The reconciliations worth keeping are the
*relationships*: per-date FX means the IBKR EUR net and the tax report's base-currency net differ by a
percent or two rather than matching exactly, and the breakdown's year total is the IBKR era plus the
estimates that precede its boundary.

**Reconciled against IBKR to 0.12%** on 2026-07-27. Compare the app against `gross_position_value`,
never net liquidation (which adds cash and accrued dividends) — "buying power" is a margin metric
derived from that same cash, not a separate bucket.

Cross-checked against IBKR via the MCP connector: IBKR lists **282 YTD trades = 218 `CASH`** (FX
conversions, correctly filtered out) **+ 64 `STK`**, and the 64 match ours symbol-for-symbol. Same-day,
same-price pairs (e.g. NU 7 @ 17.205 twice on 2026-02-04) are **genuine separate fills**, not duplicates.

**Prior years are permanently estimates here, and that is the correct answer rather than a pending
task.** The rolling window cannot reach them, but widening it would not help either: **the owner did
not use IBKR before 2026.** Everything came across by in-kind transfer from Trading 212, Scalable
Capital and Trade Republic in early 2026, so IBKR holds no 2025 executions, dividends or cash
transactions at all — a 2025 statement would generate empty. So 2025's `dividend_source =
'yfinance_estimate'` is not a gap awaiting a backfill; it is the only source that exists, and the flag
is doing exactly its job by saying so. Closed by the owner on 2026-08-17: **do not re-open it, and do
not change the Flex Query period chasing it.** (A pre-2026 realized-gains or Steuerwert figure has the
same ceiling, and for the same reason.)
