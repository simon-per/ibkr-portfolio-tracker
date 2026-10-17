/**
 * The share of the book, in percent, at which identity-unresolved value turns the Unresolved
 * card amber. A handful of ISINs no provider resolves is the normal state (about 0.3% of the
 * book), and a card amber at any value above zero was amber always — the always-present
 * warning that teaches the reader to skip it. Below this it is a neutral figure.
 */
export const UNRESOLVED_WARN_PCT = 1

export function unresolvedTone(unresolvedValue: number, totalValue: number): 'warning' | 'neutral' {
  if (!(unresolvedValue > 0) || !(totalValue > 0)) return 'neutral'
  return (unresolvedValue / totalValue) * 100 >= UNRESOLVED_WARN_PCT ? 'warning' : 'neutral'
}
