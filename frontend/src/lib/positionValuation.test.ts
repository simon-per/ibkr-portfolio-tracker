import { describe, expect, it } from 'vitest'
import { isUnpriced, isValuable } from './positionValuation'

describe('isUnpriced', () => {
  it('accepts a holding with a price and a value', () => {
    expect(isUnpriced({ market_price: 12.5, market_value_eur: 1250 })).toBe(false)
    expect(isValuable({ market_price: 12.5, market_value_eur: 1250 })).toBe(true)
  })

  it('rejects a holding with no cached price', () => {
    expect(isUnpriced({ market_price: null, market_value_eur: 0 })).toBe(true)
  })

  it('rejects a priced holding the backend could not convert', () => {
    // The clause a one-clause predicate misses, and it is not hypothetical: when the price
    // resolves but its FX rate does not, `get_positions_breakdown` zeroes the value and
    // leaves the price populated. Frankfurter cannot serve TWD at all.
    expect(isUnpriced({ market_price: 4.79, market_value_eur: 0 })).toBe(true)
  })

  it('rejects a negative value as well as a zero one', () => {
    expect(isUnpriced({ market_price: 4.79, market_value_eur: -1 })).toBe(true)
  })
})

/**
 * The family assertion, in the shape `tableFamily.test.tsx` and
 * `test_yahoo_rate_limit_family.py` use: catch the next module that rolls its own instead
 * of only the instance in front of us.
 *
 * This rule was inline in `rebalance.ts` and `currencyExposure.ts` — identical, correct,
 * and each carrying its own copy of the reasoning — and `winRate` was about to make it
 * three. Both existing copies had already needed correcting *together* on 2026-08-05, when
 * the one-clause `market_price === null` form turned out to miss the FX case, which is
 * exactly what a drifting copy would revert to. A correct copy is still a copy.
 */
describe('nothing else spells this rule out for itself', () => {
  // `?raw` rather than `node:fs`, following tableFamily.test.tsx and breakpoints.test.ts:
  // tsconfig.app.json restricts `types` to `vite/client`, so the node builtins are not
  // typed here and importing them fails `tsc -b` while the test itself passes.
  //
  // Components are scanned too, not just `lib/`. The three consumers today are all pure
  // modules by convention — the interesting mistakes here are numeric — but a card
  // deciding for itself which position counts is exactly the fourth copy this is for, and
  // scoping the guard to where the existing copies happen to live would miss it.
  const sources = import.meta.glob(['./*.ts', '../components/**/*.tsx'], {
    query: '?raw',
    import: 'default',
    eager: true,
  }) as Record<string, string>

  /** Comments are prose, and this rule is *documented* in several of them. */
  const code = (source: string) =>
    source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '')

  it('has no second definition of the unpriced predicate anywhere', () => {
    const offenders = Object.entries(sources)
      .filter(([path]) => !path.includes('positionValuation') && !path.includes('.test.'))
      .filter(([, source]) => /market_price\s*===\s*null/.test(code(source)))
      .map(([path]) => path)

    expect(
      offenders,
      `these modules test market_price themselves instead of importing isUnpriced: ` +
        `${offenders.join(', ')}. Three statistics must agree about which position is ` +
        `unvaluable — add a consumer, not a copy.`,
    ).toEqual([])
  })

  it('is actually scanning something, so the check above cannot pass vacuously', () => {
    // A glob that matches nothing satisfies the assertion above trivially — the same
    // shape as the Sharpe clamp test that asserted `abs(0) <= 10`. Both halves of the
    // pattern are pinned, since one silently matching nothing is the likelier slip.
    const paths = Object.keys(sources)
    expect(paths.some((p) => p.includes('rebalance'))).toBe(true)
    expect(paths.some((p) => p.includes('/components/'))).toBe(true)
    expect(paths.length).toBeGreaterThan(20)
  })
})
