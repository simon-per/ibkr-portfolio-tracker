import { describe, expect, it } from 'vitest'
import { CHART_OTHER, CHART_SLOTS, chartColor, chartColorFor } from './chartPalette'
import { dividendColor, hasIdentity, OTHER_COLOR, SERIES_IDENTITIES, seriesColor } from './dividendColors'
import { OTHER } from './dividendChart'

/**
 * One question, asked of the family rather than of an instance: **can a view repaint a
 * holding?** `dividendColor` takes the symbol and the server's `stack_order` and nothing
 * else, so the answer is structural — but a future edit could hand it the visible set
 * again, which is exactly how the old version worked. `dividendChart.test.ts` asks the same
 * question end to end across ranges.
 *
 * Whether the slots can be told apart is the palette's question since the dividend stack
 * moved onto it (2026-10-17): `chartPalette.test.ts` re-measures `--chart-*` out of
 * `index.css`. This file only pins that the stack uses those slots, in rank order.
 */

const order = ['AAA', 'BBB', 'CCC', 'DDD', 'EEE', 'FFF', 'GGG', 'HHH',
               'III', 'JJJ', 'KKK', 'LLL', 'MMM', 'NNN', 'OOO']

describe("a holding's colour comes from its rank and nothing else", () => {
  it('gives every identity its own colour, and the fold the Other grey', () => {
    const colors = order.slice(0, SERIES_IDENTITIES).map((s) => dividendColor(s, order))
    expect(new Set(colors).size).toBe(SERIES_IDENTITIES)
    expect(colors).not.toContain(OTHER_COLOR)
    expect(dividendColor(OTHER, order)).toBe(OTHER_COLOR)
  })

  it('folds everything past the identities, and anything it has never seen', () => {
    expect(dividendColor(order[SERIES_IDENTITIES], order)).toBe(OTHER_COLOR)
    expect(dividendColor('NOPE', order)).toBe(OTHER_COLOR)
    expect(hasIdentity(order[SERIES_IDENTITIES - 1], order)).toBe(true)
    expect(hasIdentity(order[SERIES_IDENTITIES], order)).toBe(false)
    expect(hasIdentity('NOPE', order)).toBe(false)
  })

  it('takes the order and the symbol, and nothing about the current view', () => {
    // The old signature was (symbol, stackSymbols, palette), where `stackSymbols` was the
    // top eight of the selected range — which is exactly how a range change repainted the
    // chart. Arity is a crude pin, and it is the one that would catch the regression.
    expect(dividendColor.length).toBe(2)
    for (const symbol of order.slice(0, SERIES_IDENTITIES)) {
      expect(dividendColor(symbol, order)).toBe(dividendColor(symbol, [...order]))
    }
  })

  it('is stable: rank n is palette slot n, in every theme, forever', () => {
    // The mapping a reader learns ("the biggest payer is blue") — pinned literally, so a
    // re-ordering of the slots or an off-by-one shows up here rather than as a repaint.
    expect(dividendColor('AAA', order)).toBe('var(--chart-1)')
    expect(dividendColor('BBB', order)).toBe('var(--chart-2)')
    expect(dividendColor('JJJ', order)).toBe('var(--chart-10)')
    order.slice(0, SERIES_IDENTITIES).forEach((symbol, rank) => {
      expect(dividendColor(symbol, order)).toBe(chartColor(rank))
    })
  })

  it('draws as many identities as the palette has slots, and folds to its Other', () => {
    expect(SERIES_IDENTITIES).toBe(CHART_SLOTS)
    expect(OTHER_COLOR).toBe(CHART_OTHER)
  })
})

describe('seriesColor — the one assignment dividends and the crypto donut share', () => {
  const keys = Array.from({ length: SERIES_IDENTITIES + 2 }, (_, i) => `k${i}`)

  it('is the palette assignment and the dividend assignment, not a copy of either', () => {
    expect(seriesColor).toBe(chartColorFor)
    for (const key of [...keys, 'absent']) {
      expect(seriesColor(key, keys)).toBe(dividendColor(key, keys))
    }
  })

  it('colours a key by its place in the identity order, whatever else is shown', () => {
    // The crypto order is market-cap rank: BTC stays slot 1 when SOL outgrows it by value.
    expect(seriesColor('bitcoin', ['bitcoin', 'solana'])).toBe('var(--chart-1)')
    expect(seriesColor('solana', ['bitcoin', 'solana'])).toBe('var(--chart-2)')
    expect(seriesColor('unranked', ['bitcoin'])).toBe(OTHER_COLOR)
  })
})
