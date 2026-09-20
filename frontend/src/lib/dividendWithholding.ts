const WITHHOLDING_FORMAT = new Intl.NumberFormat('en-US', {
  minimumFractionDigits: 0,
  maximumFractionDigits: 3,
})

export function formatDividendWithholdingPct(value: number): string {
  return WITHHOLDING_FORMAT.format(value)
}
