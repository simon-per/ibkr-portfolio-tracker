// @vitest-environment jsdom
import { describe, it, expect, beforeEach } from 'vitest'
import { readSelectedBenchmarks } from '@/lib/benchmarkSelection'

/**
 * `Dashboard` reads its benchmark selection from localStorage on every mount and then
 * does `selectedBenchmarks.map(...)` to build `useQueries`. A `try/catch` around
 * `JSON.parse` covers malformed JSON but not well-formed JSON of the *wrong shape* —
 * `42` and `{"a":1}` both parse cleanly, and a non-array reaching `.map` throws inside
 * the root component rather than a tab.
 *
 * The severity is what justifies the check rather than the likelihood: because the value
 * is re-read on mount, a bad one bricks the page on every reload and clearing site data
 * is the only way out. `RebalanceCard.readTargets` already draws this line for one tab.
 */

beforeEach(() => localStorage.clear())

describe('readSelectedBenchmarks', () => {
  it('returns the stored selection when it is well formed', () => {
    localStorage.setItem('selectedBenchmarks', JSON.stringify(['sp500', 'nasdaq']))
    expect(readSelectedBenchmarks()).toEqual(['sp500', 'nasdaq'])
  })

  it('returns nothing when nothing is stored', () => {
    expect(readSelectedBenchmarks()).toEqual([])
  })

  it('survives malformed JSON', () => {
    localStorage.setItem('selectedBenchmarks', '{not json')
    expect(readSelectedBenchmarks()).toEqual([])
  })

  it('refuses well-formed JSON that is not an array', () => {
    // The gap the try/catch could not see: these parse without throwing, and used to
    // be handed straight back as `string[]`.
    for (const value of ['42', '"sp500"', '{"a":1}', 'null', 'true']) {
      localStorage.setItem('selectedBenchmarks', value)
      expect(readSelectedBenchmarks()).toEqual([])
    }
  })

  it('drops members that are not strings rather than passing them to a query', () => {
    localStorage.setItem('selectedBenchmarks', JSON.stringify(['sp500', 7, null, 'nasdaq', {}]))
    expect(readSelectedBenchmarks()).toEqual(['sp500', 'nasdaq'])
  })

  it('always returns something mappable, whatever is stored', () => {
    // The property that actually matters: `.map` must never throw, because it runs
    // inside the root component and takes the whole page down.
    for (const value of ['42', '{not json', '{"a":1}', 'null', '[]', '["sp500"]']) {
      localStorage.setItem('selectedBenchmarks', value)
      expect(() => readSelectedBenchmarks().map(k => k)).not.toThrow()
    }
  })
})
