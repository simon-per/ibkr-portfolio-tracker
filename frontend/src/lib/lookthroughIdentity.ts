/**
 * How a look-through company row is named on screen: its tickers, and which of its ISINs to ask
 * the logo store for. Shared by the company table, the treemap tooltip and the insight strip,
 * so the three cannot disagree about which mark a company wears.
 */

/** Tickers only, de-duplicated: the listing keys carry their exchange ("GOOG@NASDAQ") for identity. */
export function listingTickers(listings: readonly string[]): string[] {
  return [...new Set(listings.map((l) => l.split('@')[0]).filter(Boolean))]
}

/**
 * The ISIN whose logo to show. A company folded across listings and share classes carries
 * several ISINs and the store may hold a mark for any one of them (docs/logos.md), so the
 * first one that has a stored logo wins; with none, the first ISIN — the avatar then draws
 * its monogram. `null` only for a row with no ISIN at all.
 */
export function logoIsin(
  isins: readonly string[],
  stored: Readonly<Record<string, string>>,
): string | null {
  const keys = isins.map((i) => i.trim().toUpperCase()).filter(Boolean)
  return keys.find((k) => Object.hasOwn(stored, k)) ?? keys[0] ?? null
}

/** What the monogram spells when there is no logo: the first ticker, else the name. */
export function avatarLabel(listings: readonly string[], name: string): string {
  return listingTickers(listings)[0] ?? name
}
