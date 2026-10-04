const WITHHOLDING_FORMAT = new Intl.NumberFormat('en-US', {
  minimumFractionDigits: 0,
  maximumFractionDigits: 3,
})

export function formatDividendWithholdingPct(value: number): string {
  return WITHHOLDING_FORMAT.format(value)
}

/**
 * The short on-surface label for how a forecast row was sized. Kept beside the
 * withholding formatter because the table renders the two together, as one line under
 * the amount — a qualifier behind a hover does not exist on a phone.
 */
export function forecastMethodLabel(method: string | null | undefined): string | null {
  switch (method) {
    case 'announced':
      return 'declared'
    case 'latest_payment':
      return 'latest'
    case 'same_payment_last_year':
      return 'as last yr'
    case 'estimate':
      return 'recorded'
    default:
      return null
  }
}
