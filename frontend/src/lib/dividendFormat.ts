/**
 * A headline dividend amount: `€47.50`, `CHF 1,234`.
 *
 * Cents matter here and whole units do not, until the figure gets large. This
 * portfolio earns single-digit amounts a month, so rounding 47.50 to "48" throws
 * away a meaningful part of the number; above 1000 the cents are the noise instead.
 *
 * One function for the KPI tiles, the per-year panel and the forecast card's header:
 * the rule existed twice, byte for byte, before this — and the tile and the card both
 * print "Next 12 months", where the same figure must read the same.
 * Pinned to `en-US` like every formatter in `utils.ts`.
 */
export function dividendAmount(value: number, currencySymbol: string): string {
  const decimals = Math.abs(value) < 1000 ? 2 : 0
  const digits = Math.abs(value).toLocaleString('en-US', {
    minimumFractionDigits: decimals, maximumFractionDigits: decimals,
  })
  return `${value < 0 ? '−' : ''}${currencySymbol}${digits}`
}
