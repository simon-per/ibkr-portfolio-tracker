import { describe, expect, it } from 'vitest'
import type { CryptoHistoryPoint, CryptoHoldingItem, CryptoPortfolioResponse } from './api'
import {
  allocationSlices,
  CRYPTO_RANGES,
  DONUT_COINS,
  rangePnlSeries,
  rangeStart,
  reconstructedCaption,
  reconstructedSpan,
  sliceRange,
  valueSeries,
} from './cryptoChart'
import { OTHER_COLOR } from './dividendColors'

function holding(coin_id: string, value: number | null, weight: number | null,
  over: Partial<CryptoHoldingItem> = {}): CryptoHoldingItem {
  return {
    coin_id, symbol: coin_id.toUpperCase(), name: coin_id, rank: null,
    status: value == null ? 'unpriced' : 'valued', quantity: 1, price: value,
    price_source: value == null ? null : 'coingecko', value, weight_pct: weight,
    change_today_pct: null, ...over,
  }
}

function portfolio(over: Partial<CryptoPortfolioResponse>): CryptoPortfolioResponse {
  return {
    configured: true, prices_configured: true, base_currency: 'EUR',
    as_of: '2026-09-20T12:00:00+00:00', prices_as_of: null, total_value: 1000,
    defi_value: null, cash_value: null, change_today: null, change_today_pct: null,
    pnl_since_start: null, start_date: '2026-01-01', basket_date: null,
    first_snapshot_date: null, peg_note: null, fx_unavailable: 0, valued_count: 0,
    spam_count: 0, unpriced_count: 0, unpriced_symbols: [], no_price_symbols: [],
    holdings: [], color_order: [], warnings: [], ...over,
  }
}

/** One point per day from `from` to `to` inclusive (local calendar days). */
function daily(from: string, to: string): CryptoHistoryPoint[] {
  const out: CryptoHistoryPoint[] = []
  const [y, m, d] = from.split('-').map(Number)
  for (let day = new Date(y, m - 1, d); ; day = new Date(day.getFullYear(), day.getMonth(), day.getDate() + 1)) {
    const iso = `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, '0')}-${String(day.getDate()).padStart(2, '0')}`
    out.push({ date: iso, value: 1, pnl: 1, reconstructed: false })
    if (iso === to) return out
  }
}

describe('the ranges', () => {
  it('are offered in the owner\'s order', () => {
    expect(CRYPTO_RANGES).toEqual(['1W', 'MTD', '1M', '3M', '6M', 'YTD', '1Y', 'ALL'])
  })

  it('1W is the seven days before the latest point, across a month boundary', () => {
    const points = daily('2026-09-20', '2026-10-03')
    const week = sliceRange(points, '1W')
    expect(week[0].date).toBe('2026-09-26')
    expect(week.at(-1)?.date).toBe('2026-10-03')
    expect(week).toHaveLength(8)  // seven daily moves
    // The P&L line starts at 0 on 09-26 and sums the seven days after it.
    expect(rangePnlSeries(week).at(-1)).toBe(7)
  })

  it('MTD on the 1st of a month is that one point, and still draws', () => {
    const points = daily('2026-09-20', '2026-10-01')
    const mtd = sliceRange(points, 'MTD')
    expect(mtd.map(p => p.date)).toEqual(['2026-10-01'])
    expect(rangePnlSeries(mtd)).toEqual([0])
  })

  it('MTD later in the month starts on the 1st', () => {
    const mtd = sliceRange(daily('2026-09-20', '2026-10-05'), 'MTD')
    expect(mtd[0].date).toBe('2026-10-01')
    expect(mtd).toHaveLength(5)
  })

  it('YTD starts on 1 January of the latest point\'s year', () => {
    const ytd = sliceRange(daily('2026-01-01', '2026-03-10'), 'YTD')
    expect(ytd[0].date).toBe('2026-01-01')
    expect(rangeStart('YTD', '2027-02-14')).toBe('2027-01-01')
  })

  it('never reaches before 1 January 2026: ALL and 1Y clamp to it', () => {
    const points = [
      { date: '2025-12-30', value: 1, pnl: 1, reconstructed: true },
      ...daily('2026-01-01', '2026-06-30'),
    ]
    expect(sliceRange(points, 'ALL')[0].date).toBe('2026-01-01')
    expect(sliceRange(points, '1Y')[0].date).toBe('2026-01-01')
    expect(rangeStart('1Y', '2027-06-30')).toBe('2026-06-30')
  })

  it('counts the month ranges back from the latest point', () => {
    expect(rangeStart('1M', '2026-10-03')).toBe('2026-09-03')
    expect(rangeStart('6M', '2026-10-03')).toBe('2026-04-04')
  })

  it('is empty for an empty series', () => {
    expect(sliceRange([], '1W')).toEqual([])
  })
})

describe('valueSeries', () => {
  it('keeps an unknown day as a gap — the series is daily, so a null is a real gap', () => {
    const points: CryptoHistoryPoint[] = [
      { date: '2026-09-01', value: 100, pnl: 1, reconstructed: false },
      { date: '2026-09-02', value: null, pnl: null, reconstructed: false },
      { date: '2026-09-03', value: 110, pnl: 4, reconstructed: false },
    ]
    expect(valueSeries(points)).toEqual([100, null, 110])
  })
})

describe('rangePnlSeries', () => {
  const day = (date: string, pnl: number | null) => ({ date, value: 1, pnl, reconstructed: false })

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

  it('is empty for an empty range', () => {
    expect(rangePnlSeries([])).toEqual([])
  })
})

describe('the reconstructed span', () => {
  it('is the first and last reconstructed day inside the slice', () => {
    const points = daily('2026-08-20', '2026-08-26').map(p => ({
      ...p, reconstructed: p.date < '2026-08-24',
    }))
    expect(reconstructedSpan(points)).toEqual({ from: '2026-08-20', to: '2026-08-23' })
    expect(reconstructedSpan(points.slice(4))).toBeNull()
  })

  it('names the backfilled and the rebuilt days in one sentence', () => {
    const id = (iso: string) => iso
    expect(reconstructedCaption('2026-01-01', '2026-08-23', '2026-10-02', id)).toBe(
      "Reconstructed (shaded) — 2026-01-01 – 2026-08-22: the 2026-08-23 coins at each day's " +
      "price; 2026-08-23 – 2026-10-01: holdings rebuilt from CoinStats' transactions.",
    )
    // Before the rebuild has run, the first synced day's basket stands in for every day.
    expect(reconstructedCaption('2026-01-01', '2026-10-02', '2026-10-02', id)).toBe(
      "Reconstructed (shaded) — 2026-01-01 – 2026-10-01: the 2026-10-02 coins at each day's price.",
    )
    expect(reconstructedCaption('2026-01-01', null, null, id)).toBeNull()
  })
})

describe('allocationSlices', () => {
  it('draws shares of the priced total', () => {
    const slices = allocationSlices(portfolio({
      holdings: [holding('bitcoin', 600, 66.67), holding('solana', 300, 33.33)],
      color_order: ['bitcoin', 'solana'],
    }))
    expect(slices.map(s => [s.key, s.pct])).toEqual([['bitcoin', 66.67], ['solana', 33.33]])
  })

  it('colours by identity order, not by size', () => {
    // SOL outgrew BTC by value, but BTC is first in the identity order and keeps slot 1.
    const slices = allocationSlices(portfolio({
      holdings: [holding('solana', 700, 70), holding('bitcoin', 200, 20)],
      color_order: ['bitcoin', 'solana'],
    }))
    expect(slices.find(s => s.key === 'bitcoin')?.color).toBe('var(--viz-series-1)')
    expect(slices.find(s => s.key === 'solana')?.color).toBe('var(--viz-series-2)')
  })

  it('folds the tail into Other and never draws an unpriced coin', () => {
    const many = Array.from({ length: DONUT_COINS + 3 }, (_, i) =>
      holding(`c${i}`, 100 - i, 9 - i * 0.5))
    const slices = allocationSlices(portfolio({
      holdings: [...many, holding('mystery', null, null),
        holding('noprice', null, null, { status: 'no_price' })],
      color_order: many.map(h => h.coin_id),
    }))
    const coins = slices.filter(s => s.kind === 'coin')
    expect(coins).toHaveLength(DONUT_COINS)
    const other = slices.find(s => s.kind === 'other')
    expect(other?.color).toBe(OTHER_COLOR)
    expect(other?.value).toBe(many.slice(DONUT_COINS).reduce((n, h) => n + (h.value ?? 0), 0))
    expect(slices.some(s => s.key === 'mystery' || s.key === 'noprice')).toBe(false)
  })

  it('says an Other share is unknown when a folded coin has no weight', () => {
    const slices = allocationSlices(portfolio({
      holdings: [holding('a', 10, 1), holding('b', 5, null)],
      color_order: ['a'],
    }))
    expect(slices.find(s => s.kind === 'other')?.pct).toBeNull()
  })
})
