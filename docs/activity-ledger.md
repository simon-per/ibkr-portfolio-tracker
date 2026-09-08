# The Activity ledger

> Moved verbatim from `CLAUDE.md` on 2026-09-08, when that file became a short index so a
> session no longer loads ~92k tokens of documentation to start. **This file is
> authoritative for its subsystem**: every invariant here was a bug first. Keep it accurate
> the way CLAUDE.md was kept — update it in the same change that moves the code it describes.

## Activity ledger

`GET /api/portfolio/activity` (+ `.csv`) → `ActivityService`, rendered by `ActivityTab.tsx`. It unions
**trades, corporate actions, cash flows and dividends** into one chronological list with date-range,
kind and symbol filters.

It exists because all four tables were ingested, reconciled and depended on — the tax report reads
`trades`, the contributions splice reads `cash_flows`, `reconcile_taxlots` reads `corporate_actions` —
with **no read surface at all**. The sharpest consequence: the transfer audit this file prescribes
before trusting any money-added figure was `manage_cash_flows list` over ssh. Every cash row now
carries `counts_as_money_in`, badged *Transfer · not money in*.

Five rules, each of which would be a bug the other way:

- **Paging is applied to the merged list, not per table.** The four sources are separately ordered, so
  a per-table limit would silently drop every dividend in a busy trading month.
- **Dividends are era-spliced, exactly as every computing reader does it** — and this one was missing
  until 2026-08-05, which is the whole reason the rule is written here now. The same dividend is stored
  twice, yfinance under its ex-date and IBKR under its pay date a week or two apart, so without the
  splice the ledger listed **both**: 31 duplicate rows and a dividend total of 113 CHF against a real
  65, overstated **72%**. Nothing the app *computes* was affected — the breakdown, the summary card,
  XIRR and the tax report all splice — which is exactly why it survived: the only wrong surface was the
  one that merely displays.

  **And it calls `_splice_by_era` rather than reimplementing its rule**, which it did
  not until 2026-08-05. The inline copy was correct the day it was written and silently
  wrong two days later, when the helper gained its boundary-duplicate match: every other
  reader stopped showing the pair and the ledger kept showing it. `_splice_by_era` takes
  an explicit `boundary` so the windowing caller can comply, and the fetch widens by
  `EX_TO_PAY_MAX_LAG_DAYS` on both sides — the IBKR row that pairs with a windowed
  estimate can fall outside the window even when the estimate does not, so asking for
  1–15 February would otherwise resurrect the duplicate. `test_era_splice_boundary.py`
  fails any service that reads dividend rows without reaching the helper.

  **The boundary must come from the whole table, not the window.** `_splice_by_era` derives
  `min(ibkr_dates)` from the rows handed to it, which is right for readers that splice the full history
  and wrong here, because the ledger windows *first*: fed a slice, a window opening after the era began
  would treat its own earliest IBKR row as the era start and resurrect superseded estimates. Hence
  `DividendRepository.earliest_ibkr_payment_date()` — same shape and same reason as
  `CashFlowRepository.earliest_flow_date()`. Do not "simplify" this to
  `_splice_by_era(get_between(...))`; that is the obvious-looking form and it is the bug.
  Pre-boundary estimates are still kept and still badged — dropping those is the mirror-image bug, and
  it once blanked every pre-IBKR month from the dividend card.
- **Dividends are dated by `pay_date` falling back to `ex_date`** — the same `coalesce`
  `has_ibkr_dividends` uses. yfinance stores under the ex-date and IBKR under the pay date, and
  Mastercard's 29-day lag exceeds a monthly cycle, so the column asked decides the window.
- **Amounts convert at each row's own date** through the same `BaseFx` every other read endpoint uses.
- **A field a kind cannot fill is `None`, never 0** — in JSON and in the CSV. A corporate action has no
  price, and a `0.00` would assert one.

Zero-value dividend rows are excluded on the same test the two dividend readers use, so yfinance's
pre-ownership history never surfaces. Tests: `tests/test_activity_service.py`, plus cases in
`tests/test_api_smoke.py` — including one comparing the ledger's dividend total against
`DividendService`'s over the same span, since two readers of one table that nothing compares is how
the 72% got there.

---
