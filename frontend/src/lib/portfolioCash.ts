import type { CashSource } from './api'

/**
 * "Does this payload carry a cash balance worth showing?" — asked by the value chart,
 * the summary cards, the positions table and the allocation charts.
 *
 * It lives here for the reason `positionValuation.ts` does: four surfaces needed the
 * same predicate, and the two that already shared one in this codebase had drifted
 * apart the first time the rule changed. Read `api.ts` for what each `CashSource`
 * means; this file decides only whether a balance may be *presented as one*.
 *
 * Two states answer no, and neither may be rendered as a zero balance:
 *
 * - **The field is absent.** A backend older than 2026-08-26 does not send it. Reading
 *   that as "no cash" would put a confident 0.00 on every screen served by an older
 *   deploy — the same backward-compatible refusal `unpriced_holdings` and
 *   `external_flow_eur` both make about their own absence.
 * - **`cash_source === 'unknown'`.** No ledger holds a row to derive a balance from, so
 *   cash genuinely computes to 0.00 everywhere — and that is exactly why it must not be
 *   shown. Labelling a holdings-only line "Total Value" over it would assert a
 *   completeness nothing established. The backend says the same when a trade has no FX
 *   rate (`cash_unconverted` > 0): the sum is short by the whole trade (GitHub #59).
 *
 * A **derived zero** is a different thing and does qualify: it is a real answer for an
 * account that has deployed everything it has. The distinction between "measured zero"
 * and "no measurement" is this codebase's most repeated bug, and this is where it is
 * decided for cash.
 */
export function cashIsTracked(
  carrier: { cash_source?: CashSource } | null | undefined
): boolean {
  return !!carrier && carrier.cash_source != null && carrier.cash_source !== 'unknown'
}

/**
 * The part of the balance that joins holdings in a "% of portfolio" denominator: a
 * tracked, **positive** balance, else 0. A margin debit is left out rather than
 * subtracted (shrinking the denominator around it inflates every holding), and an
 * untracked one adds nothing.
 *
 * The client mirror of the backend's `UninvestedCash.in_book` (`cash_service.py`), which
 * Allocation and Look-through divide by — so a holding reads the same share on Positions
 * as on those two tabs in every cash state (GitHub #52). The summary ships only the signed
 * balance, hence the copy; `portfolioCash.test.ts` and `test_uninvested_cash_family.py`
 * pin the same four cases, and a change to one side changes both.
 */
export function inBookCash(
  cash: { amount: number; cash_source?: CashSource } | null | undefined
): number {
  return cashIsTracked(cash) && cash!.amount > 0 ? cash!.amount : 0
}
