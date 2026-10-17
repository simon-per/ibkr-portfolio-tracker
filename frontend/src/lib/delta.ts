/**
 * Shared rules for rendering a percentage change.
 *
 * Extracted from ContributionsStrip, which owned the only copy: a second colour
 * rule elsewhere in the app would be free to disagree with it about what counts
 * as flat, and two different answers to "is this up or down" is worse than
 * either answer.
 */

/**
 * Changes inside this band are ordinary variation, not a trend worth colouring.
 *
 * Dividend income is lumpy — a payer shifting its pay date by a few days moves a
 * month by more than this — so a small move gets the muted treatment rather than
 * a confident green or red.
 */
export const FLAT_BAND_PCT = 5

/**
 * The band is a parameter because it is a claim about the *series*, not about
 * percentages in general. It suits dividend income and monthly contributions, where a
 * few percent is cadence noise. It does not suit a portfolio's period return, where 3%
 * is a real move and grepping it out as "flat" would understate it — those callers
 * pass 0.
 */
export function deltaColor(pct: number, flatBand: number = FLAT_BAND_PCT): string {
  const tone = deltaTone(pct, flatBand)
  if (tone === 'up') return 'text-green-600 dark:text-green-400'
  if (tone === 'down') return 'text-red-600 dark:text-red-400'
  return 'text-muted-foreground'
}

/** The one up / down / flat decision; the text colour and the pill's wash both read it. */
export function deltaTone(
  pct: number | null | undefined, flatBand: number = FLAT_BAND_PCT,
): 'up' | 'down' | 'flat' {
  if (pct == null || !Number.isFinite(pct)) return 'flat'
  if (pct > flatBand) return 'up'
  if (pct < -flatBand) return 'down'
  return 'flat'
}

/**
 * Signed percentage, or an em dash when there is nothing to compare against.
 *
 * `null` is the backend saying the base was zero, which happens constantly here:
 * a quarterly payer pays nothing in most months. Growth against zero is
 * undefined rather than large, so it must never render as a number.
 */
export function formatDelta(pct: number | null | undefined): string {
  if (pct == null || !Number.isFinite(pct)) return '—'
  const rounded = Math.abs(pct) >= 100 ? Math.round(pct) : Math.round(pct * 10) / 10
  return `${rounded > 0 ? '+' : ''}${rounded}%`
}

/** '▲' / '▼' / '' — omitted inside the flat band, where direction is noise. */
export function deltaArrow(pct: number | null | undefined, flatBand: number = FLAT_BAND_PCT): string {
  const tone = deltaTone(pct, flatBand)
  return tone === 'up' ? '▲' : tone === 'down' ? '▼' : ''
}
