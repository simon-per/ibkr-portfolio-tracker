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
 *   completeness nothing established.
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
 * How a tracked balance should be qualified in prose, or `null` when it needs no
 * qualifier.
 *
 * `derived` is ours rather than the broker's: it is computed from the trade, deposit
 * and dividend ledgers and so cannot see broker interest, account fees or the spread on
 * an FX conversion. On this account that is a couple of hundred francs against a
 * five-figure balance — small enough to show, too systematic to leave unsaid, because
 * a figure people reconcile against a broker statement has to declare when it is not
 * the broker's figure.
 */
export function cashCaveat(source: CashSource | undefined): string | null {
  return source === 'derived'
    ? 'derived from your trade, deposit and dividend history — it excludes broker interest and fees'
    : null
}
