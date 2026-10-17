import { readStored } from './storage'
import type { TargetMap } from './rebalance'

export const TARGETS_KEY = 'allocationTargets'

/**
 * Targets live in localStorage, following `ForecastTab`'s precedent for
 * user-set parameters. Deliberately not a table and an endpoint: `/api/` is
 * proxied publicly and every write is auth-gated, so a new mutating route is a
 * bigger surface than this feature earns. It also means switching machines
 * loses them, which is the trade being made.
 */
export function readTargets(): TargetMap {
  try {
    const raw = readStored(TARGETS_KEY)
    if (!raw) return {}
    const parsed: unknown = JSON.parse(raw)
    if (typeof parsed !== 'object' || parsed === null) return {}
    const out: TargetMap = {}
    for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
      const id = Number(key)
      // A stored target that is not a usable percent is dropped rather than
      // coerced: a NaN would propagate into every drift on the page.
      if (!Number.isInteger(id) || typeof value !== 'number') continue
      if (!Number.isFinite(value) || value < 0 || value > 100) continue
      out[id] = value
    }
    return out
  } catch {
    // Corrupt JSON must not take the Allocation tab down with it.
    return {}
  }
}
