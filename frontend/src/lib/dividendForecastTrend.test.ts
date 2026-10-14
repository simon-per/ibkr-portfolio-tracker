import { describe, expect, it } from 'vitest'
import type {
  DividendForecastChange,
  DividendForecastHistoryPoint,
  DividendForecastHistoryResponse,
} from './api'
import { forecastTrend, partLabel } from './dividendForecastTrend'

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
    since: '2026-08-28', shares: 0, payout: 0, newly_forecast: 0, fx: 0,
    unattributed: 0, ...extra,
  }
}

function history(
  points: DividendForecastHistoryPoint[],
  growthFrom: string | null = '2026-08-28',
): DividendForecastHistoryResponse {
  return { base_currency: 'CHF', growth_from: growthFrom, points }
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

  it('leaves out the parts that round to nothing, the residual included', () => {
    const trend = forecastTrend(history([
      point('2026-08-28', 300),
      point('2026-10-13', 360, {
        breakdown: change({ shares: 52.4, payout: 9.1, fx: -1.6, unattributed: 0.01 }),
      }),
    ]))
    expect(trend?.parts.map((p) => [p.key, p.value])).toEqual([
      ['shares', 52.4], ['payout', 9.1], ['fx', -1.6],
    ])
  })

  it('shows the residual once it is material', () => {
    const trend = forecastTrend(history([
      point('2026-08-28', 300),
      point('2026-10-13', 360, { breakdown: change({ shares: 52, unattributed: 8 }) }),
    ]))
    expect(trend?.parts.map((p) => p.key)).toEqual(['shares', 'unattributed'])
  })

  it('has no parts when the latest day carries no breakdown', () => {
    const trend = forecastTrend(history([point('2026-08-28', 300), point('2026-08-29', 301)]))
    expect(trend?.parts).toEqual([])
    expect(trend?.partsSince).toBeNull()
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

describe('partLabel', () => {
  it('says sold when the share count fell', () => {
    expect(partLabel({ key: 'shares', label: 'shares bought', value: -12 })).toBe('shares sold')
    expect(partLabel({ key: 'shares', label: 'shares bought', value: 12 })).toBe('shares bought')
    expect(partLabel({ key: 'fx', label: 'currency', value: -12 })).toBe('currency')
  })
})
