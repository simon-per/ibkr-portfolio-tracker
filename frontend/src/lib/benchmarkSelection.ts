import { readStored } from './storage'

export const BENCHMARKS_KEY = 'selectedBenchmarks'

/**
 * The stored benchmark selection, validated rather than trusted.
 *
 * `JSON.parse` was already wrapped in a try/catch, which covers malformed JSON but
 * not *well-formed JSON of the wrong shape*: `42` and `{"a":1}` both parse cleanly
 * and were then handed back as `string[]`. `selectedBenchmarks.map(...)` feeds
 * `useQueries` a few lines below, so a non-array throws inside this component — the
 * whole dashboard, not one tab — and because the bad value is re-read on every mount,
 * reloading cannot recover it. Clearing site data would be the only way out.
 *
 * Same shape and reasoning as `RebalanceCard`'s `readTargets`, which drops entries it
 * cannot use instead of coercing them. Non-string members go too: they would reach the
 * benchmark query as keys and fetch nothing.
 */
export function readSelectedBenchmarks(): string[] {
  try {
    const raw = readStored(BENCHMARKS_KEY)
    if (!raw) return []
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed.filter((k): k is string => typeof k === 'string')
  } catch {
    return []
  }
}
