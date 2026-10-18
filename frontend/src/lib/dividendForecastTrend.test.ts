import { describe, expect, it } from 'vitest'
import type {
  DividendForecastChange,
  DividendForecastHistoryPoint,
  DividendForecastHistoryResponse,
} from './api'
import { forecastTrend, fromBuysLabel, fromRaisesLabel, utcToday } from './dividendForecastTrend'

function point(
  date: string,
  value: number,
  extra: Partial<DividendForecastHistoryPoint> = {},
): DividendForecastHistoryPoint {
  return {
    date, next_12m_eur: value, ttm_net_eur: 50,
    next_12m_change_pct: null, next_12m_mtd_pct: null, next_12m_ytd_pct: null,
    mtd_from: null, ytd_from: null, next_12m_since_start_pct: null, breakdown: null,
    reconstructed: false, ...extra,
  }
}

function change(extra: Partial<DividendForecastChange> = {}): DividendForecastChange {
  return {
    since: '2026-08-28', shares: 0, raises: 0, timing: 0, newly_forecast: 0, fx: 0,
    unattributed: 0, ...extra,
  }
}

function history(
  points: DividendForecastHistoryPoint[],
  growthFrom: string | null = '2026-08-28',
): DividendForecastHistoryResponse {
  return { base_currency: 'CHF', growth_from: growthFrom, points, raises: [] }
}

describe('forecastTrend', () => {
  it('draws nothing before the day growth is measured from', () => {
    const trend = forecastTrend(history([
      point('2026-08-20', 50),
      point('2026-08-28', 300, { reconstructed: true }),
      point('2026-10-13', 360, { next_12m_since_start_pct: 20 }),
    ]))
    expect(trend?.points.map((p) => p.date)).toEqual(['2026-08-28', '2026-10-13'])
    expect(trend?.start).toBe('2026-08-28')
    expect(trend?.latest).toBe(360)
    expect(trend?.sincePct).toBe(20)
  })

  it('draws the whole series when no start day is set', () => {
    const trend = forecastTrend(history([point('2026-08-20', 50), point('2026-08-21', 55)], null))
    expect(trend?.points).toHaveLength(2)
    expect(trend?.start).toBe('2026-08-20')
  })

  it('carries the income received beside the forecast on every point', () => {
    const trend = forecastTrend(history([
      point('2026-08-28', 300, { ttm_net_eur: 120 }),
      point('2026-10-13', 360, { ttm_net_eur: 150 }),
    ]))
    expect(trend?.points.map((p) => p.received)).toEqual([120, 150])
  })

  it('takes the month and the year growth off the newest day, null when it has none', () => {
    const trend = forecastTrend(history([
      point('2026-08-28', 300),
      point('2026-10-13', 360, { next_12m_mtd_pct: 2.5, next_12m_ytd_pct: 20 }),
    ]))
    expect(trend?.mtdPct).toBe(2.5)
    expect(trend?.ytdPct).toBe(20)
    expect(forecastTrend(history([point('2026-08-28', 300)]))?.mtdPct).toBeNull()
  })

  it('puts what shares bought and raises added in the header, not in the parts', () => {
    const trend = forecastTrend(history([
      point('2026-08-28', 300),
      point('2026-10-13', 360, {
        breakdown: change({
          shares: 52.4, raises: 6.3, timing: -4.7, fx: -1.6, unattributed: 0.01,
        }),
      }),
    ]))
    expect(trend?.fromBuys).toBe(52.4)
    expect(trend?.fromRaises).toBe(6.3)
    // The parts that round to nothing stay out, the residual included.
    expect(trend?.parts.map((p) => [p.key, p.value])).toEqual([['timing', -4.7], ['fx', -1.6]])
  })

  it('reports a cut as a negative raise, and no raise figure when it rounds to nothing', () => {
    const cut = forecastTrend(history([
      point('2026-08-28', 300),
      point('2026-10-13', 290, { breakdown: change({ raises: -10 }) }),
    ]))
    expect(cut?.fromRaises).toBe(-10)
    const tiny = forecastTrend(history([
      point('2026-08-28', 300),
      point('2026-10-13', 300.3, { breakdown: change({ raises: 0.3 }) }),
    ]))
    expect(tiny?.fromRaises).toBeNull()
    expect(forecastTrend(history([point('2026-08-28', 300), point('2026-08-29', 301)]))
      ?.fromRaises).toBeNull()
  })

  it('has no from-buys figure without a breakdown, or when it rounds to nothing', () => {
    expect(forecastTrend(history([point('2026-08-28', 300), point('2026-08-29', 301)]))?.fromBuys)
      .toBeNull()
    const tiny = forecastTrend(history([
      point('2026-08-28', 300),
      point('2026-08-29', 301, { breakdown: change({ shares: 0.2, timing: 0.8 }) }),
    ]))
    expect(tiny?.fromBuys).toBeNull()
  })

  it('shows the residual once it is material', () => {
    const trend = forecastTrend(history([
      point('2026-08-28', 300),
      point('2026-10-13', 360, { breakdown: change({ shares: 52, unattributed: 8 }) }),
    ]))
    expect(trend?.parts.map((p) => p.key)).toEqual(['unattributed'])
  })

  it('names the day the breakdown starts on only when it is later than the line', () => {
    const later = forecastTrend(history([
      point('2026-08-28', 300),
      point('2026-10-13', 360, { breakdown: change({ since: '2026-09-05', shares: 40 }) }),
    ]))
    expect(later?.partsSince).toBe('2026-09-05')

    const same = forecastTrend(history([
      point('2026-08-28', 300),
      point('2026-10-13', 360, { breakdown: change({ shares: 40 }) }),
    ]))
    expect(same?.partsSince).toBeNull()
  })

  it('is null without a history or without a day from the start on', () => {
    expect(forecastTrend(undefined)).toBeNull()
    expect(forecastTrend(history([]))).toBeNull()
    expect(forecastTrend(history([point('2026-08-01', 40)]))).toBeNull()
  })
})

/**
 * The tile above the card shows the breakdown's live figure; the card used to show the
 * newest snapshot under the same label. The live figure is today's point now.
 */
describe('forecastTrend with the live figure', () => {
  const live = { next12m: 372, ttm: 155, currency: 'CHF' }
  const series = () => history([
    point('2026-08-28', 300),
    point('2026-09-30', 340, { mtd_from: '2026-08-31', ytd_from: '2026-08-28' }),
    point('2026-10-13', 360, {
      mtd_from: '2026-09-30', ytd_from: '2026-08-28',
      next_12m_mtd_pct: 5.88, next_12m_ytd_pct: 20, next_12m_since_start_pct: 20,
    }),
  ])

  it('replaces the newest snapshot when it is today’s, and measures from the same anchors', () => {
    const trend = forecastTrend(series(), live, '2026-10-13')!
    expect(trend.latest).toBe(372)
    expect(trend.points.at(-1)).toEqual({ date: '2026-10-13', value: 372, received: 155 })
    expect(trend.points).toHaveLength(3)
    expect(trend.sincePct).toBe(24)          // 372 over the start day's 300
    expect(trend.mtdPct).toBe(9.41)          // over 2026-09-30's 340, two decimals
    expect(trend.ytdPct).toBe(24)
  })

  it('appends today after an older snapshot, keeping the month’s anchor', () => {
    const trend = forecastTrend(series(), live, '2026-10-14')!
    expect(trend.points.map((p) => p.date)).toEqual([
      '2026-08-28', '2026-09-30', '2026-10-13', '2026-10-14',
    ])
    expect(trend.points[2].value).toBe(360)  // history stays history
    expect(trend.mtdPct).toBe(9.41)
  })

  it('measures a new month from the newest snapshot, the last one before it began', () => {
    const trend = forecastTrend(series(), live, '2026-11-01')!
    expect(trend.mtdPct).toBe(3.33)          // 372 over 2026-10-13's 360
    expect(trend.ytdPct).toBe(24)            // the year has not turned
  })

  it('has no growth on a series whose only day is today', () => {
    const trend = forecastTrend(history([point('2026-10-13', 360)]), live, '2026-10-13')!
    expect(trend.latest).toBe(372)
    expect(trend.sincePct).toBeNull()
    expect(trend.mtdPct).toBeNull()
    expect(trend.ytdPct).toBeNull()
  })

  it('measures a second day from the first when the first was its own anchor', () => {
    const trend = forecastTrend(history([point('2026-10-13', 360)]), live, '2026-10-14')!
    expect(trend.sincePct).toBe(3.33)
    expect(trend.mtdPct).toBe(3.33)
  })

  it('ignores a live figure in another currency than the history', () => {
    const trend = forecastTrend(series(), { ...live, currency: 'EUR' }, '2026-10-13')!
    expect(trend.latest).toBe(360)
    expect(trend.sincePct).toBe(20)
    expect(trend.mtdPct).toBe(5.88)
  })
})

describe('fromBuysLabel', () => {
  it('says sales when the share count fell', () => {
    expect(fromBuysLabel(12)).toBe('from buys')
    expect(fromBuysLabel(-12)).toBe('from sales')
  })
})

describe('fromRaisesLabel', () => {
  it('says cuts when the payers cut more than they raised', () => {
    expect(fromRaisesLabel(6)).toBe('from raises')
    expect(fromRaisesLabel(-6)).toBe('from cuts')
  })
})

describe('utcToday', () => {
  it('is the UTC date the server keys snapshots on, not the local one', () => {
    expect(utcToday(new Date('2026-10-14T23:30:00-05:00'))).toBe('2026-10-15')
  })
})
