import type { EarningsHistoryItem } from './api'

/**
 * How many reported quarters beat the consensus, out of those with a verdict at all.
 *
 * A row without `beat_or_miss` (no estimate to compare against) is neither a beat nor a
 * miss, so it stays out of the denominator rather than counting as a miss. Null when no
 * row has a verdict: "0 of 0" is not a beat rate.
 */
export function earningsBeats(rows: readonly EarningsHistoryItem[]): { beats: number; judged: number } | null {
  const judged = rows.filter(r => r.beat_or_miss != null)
  if (judged.length === 0) return null
  return { beats: judged.filter(r => r.beat_or_miss === 'Beat').length, judged: judged.length }
}
