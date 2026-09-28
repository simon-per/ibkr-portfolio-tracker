import { describe, expect, it } from 'vitest'
import type { CryptoHoldingItem, CryptoPortfolioResponse } from './api'
import { allocationSlices, DONUT_COINS, rangePnlSeries, sliceRange, UNITEMISED_COLOR, valueSeries } from './cryptoChart'
import { OTHER_COLOR } from './dividendColors'

function holding(coin_id: string, value: number | null, weight: number | null,
  over: Partial<CryptoHoldingItem> = {}): CryptoHoldingItem {
  return {
    coin_id, symbol: coin_id.toUpperCase(), name: coin_id, rank: null, is_fiat: false,
    status: value == null ? 'unpriced' : 'valued', quantity: 1, price: value, value,
    weight_pct: weight, change_24h_pct: null, avg_buy: null, total_cost: null,
    unrealized_pl: null, unrealized_pl_pct: null, realized_pl: null, ...over,
  }
}

function portfolio(over: Partial<CryptoPortfolioResponse>): CryptoPortfolioResponse {
  return {
    configured: true, base_currency: 'EUR', as_of: '2026-09-20T12:00:00+00:00',
    total_value: 1000, itemised_value: 900, unitemised_value: 100, defi_value: null,
    total_cost: null, unrealized_pl: null, unrealized_pl_pct: null, realized_pl: null,
    realized_pl_pct: null, all_time_pl: null, all_time_pl_pct: null, change_24h: null,
    change_24h_pct: null, fx_caveat: null, fx_unavailable: 0, valued_count: 0,
    spam_count: 0, unpriced_count: 0, unpriced_symbols: [], holdings: [], color_order: [],
    warnings: [], ...over,
  }
}

describe('sliceRange', () => {
  const points = ['2025-01-01', '2026-06-01', '2026-09-01', '2026-09-20'].map(date => ({
    date, value: 1, pnl: null,
  }))
  const today = new Date(2026, 8, 20)

  it('keeps the whole series for ALL — no five-year cap', () => {
    expect(sliceRange(points, 'ALL', today)).toHaveLength(4)
  })

  it('cuts at local calendar days before today', () => {
    expect(sliceRange(points, '1M', today).map(p => p.date)).toEqual(['2026-09-01', '2026-09-20'])
    expect(sliceRange(points, '6M', today).map(p => p.date)).toEqual([
      '2026-06-01', '2026-09-01', '2026-09-20',
    ])
  })
})

describe('valueSeries', () => {
  it('keeps only the sampled days, so the line is drawn through the samples', () => {
    const merged = [
      { date: '2026-09-01', value: 100, pnl: 1 },
      { date: '2026-09-02', value: null, pnl: 2 },
      { date: '2026-09-03', value: null, pnl: 3 },
      { date: '2026-09-04', value: 110, pnl: 4 },
    ]
    expect(valueSeries(merged).map(p => p.date)).toEqual(['2026-09-01', '2026-09-04'])
  })
})

describe('rangePnlSeries', () => {
  // CoinStats serves one day's P&L per point (measured 2026-09-28), not a running total.
  const day = (date: string, pnl: number | null) => ({ date, value: 1, pnl })

  it('starts at exactly 0 and sums the daily amounts after the first day', () => {
    expect(rangePnlSeries([day('2026-09-01', 40), day('2026-09-02', 10), day('2026-09-03', -25)]))
      .toEqual([0, 10, -15])
  })

  it('over a slice, measures from the first day of that slice', () => {
    const all = [day('2026-09-01', 5), day('2026-09-02', 7), day('2026-09-03', 11)]
    expect(rangePnlSeries(all.slice(1))).toEqual([0, 11])
  })

  it('makes every point after an unknown day unknown rather than counting it as 0', () => {
    expect(rangePnlSeries([day('2026-09-01', 1), day('2026-09-02', null), day('2026-09-03', 3)]))
      .toEqual([0, null, null])
  })

  it('lets the live snapshot, which has no daily P&L, only end the line', () => {
    expect(rangePnlSeries([day('2026-09-01', 1), day('2026-09-02', 2), day('2026-09-28', null)]))
      .toEqual([0, 2, null])
  })

  it('is empty for an empty range', () => {
    expect(rangePnlSeries([])).toEqual([])
  })
})

describe('allocationSlices', () => {
  it('draws shares of the whole total, with the unitemised part as its own slice', () => {
    const slices = allocationSlices(portfolio({
      holdings: [holding('bitcoin', 600, 60), holding('solana', 300, 30)],
      color_order: ['bitcoin', 'solana'],
    }))
    expect(slices.map(s => [s.key, s.pct])).toEqual([
      ['bitcoin', 60], ['solana', 30], ['__unitemised', 10],
    ])
    expect(slices.at(-1)?.color).toBe(UNITEMISED_COLOR)
    // Nothing renormalised: the three shares still sum to the whole.
    expect(slices.reduce((n, s) => n + (s.pct ?? 0), 0)).toBe(100)
  })

  it('colours by identity order, not by size', () => {
    // SOL outgrew BTC by value, but BTC is first in the identity order and keeps slot 1.
    const slices = allocationSlices(portfolio({
      holdings: [holding('solana', 700, 70), holding('bitcoin', 200, 20)],
      color_order: ['bitcoin', 'solana'], unitemised_value: 0,
    }))
    expect(slices.find(s => s.key === 'bitcoin')?.color).toBe('var(--viz-series-1)')
    expect(slices.find(s => s.key === 'solana')?.color).toBe('var(--viz-series-2)')
  })

  it('folds the tail into Other and never draws an unpriced coin', () => {
    const many = Array.from({ length: DONUT_COINS + 3 }, (_, i) =>
      holding(`c${i}`, 100 - i, 9 - i * 0.5))
    const slices = allocationSlices(portfolio({
      holdings: [...many, holding('mystery', null, null)],
      color_order: many.map(h => h.coin_id), unitemised_value: null,
    }))
    const coins = slices.filter(s => s.kind === 'coin')
    expect(coins).toHaveLength(DONUT_COINS)
    const other = slices.find(s => s.kind === 'other')
    expect(other?.color).toBe(OTHER_COLOR)
    expect(other?.value).toBe(many.slice(DONUT_COINS).reduce((n, h) => n + (h.value ?? 0), 0))
    expect(slices.some(s => s.key === 'mystery')).toBe(false)
  })

  it('says an Other share is unknown when a folded coin has no weight', () => {
    const slices = allocationSlices(portfolio({
      holdings: [holding('a', 10, 1), holding('b', 5, null)],
      color_order: ['a'], unitemised_value: 0,
    }))
    expect(slices.find(s => s.kind === 'other')?.pct).toBeNull()
  })
})
