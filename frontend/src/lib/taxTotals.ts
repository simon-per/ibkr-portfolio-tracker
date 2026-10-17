/**
 * The tax report's dividend totals as the Tax tab prints them. Pure, so the arithmetic of
 * the labels is tested in node and `TaxTab.tsx` exports only its component.
 */

export type Money = (n: number) => string

/** Withholding is a deduction, so a recorded amount prints with its minus. */
export function withheld(money: Money, amount: number): string {
  return amount ? `-${money(amount)}` : money(0)
}

/**
 * The withholding total's cell. An estimate has a guessed gross and *no* withholding on
 * record, so the server's total is the recorded withholding only — `null` when every
 * row is an estimate — and `estimated_rows` is the count it leaves out. Both are read
 * from the response: the server already counted, and a second count here is a copy.
 */
export function withholdingTotalLabel(
  money: Money,
  total: number | null,
  estimatedRows: number,
): { amount: string; excluded: string | null } {
  if (total == null) return { amount: '—', excluded: null }
  return {
    amount: withheld(money, total),
    excluded: estimatedRows > 0 ? `excl. ${estimatedRows} est.` : null,
  }
}

/** The tag on a total that includes estimated rows (gross and net), when only some are. */
export function includedEstimatesTag(estimatedRows: number, rowCount: number): string | null {
  return estimatedRows > 0 && estimatedRows < rowCount ? `incl. ${estimatedRows} est.` : null
}
