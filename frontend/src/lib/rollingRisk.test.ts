import { describe, expect, it } from 'vitest'
import { drawdownEpisodes, rollingMetrics, MIN_WINDOW_RETURNS } from './rollingRisk'
import { sharpeRatio, annualizedVolatilityPct, type ValueSeriesPoint } from './portfolioKpis'

/** Weekday-free for simplicity: one point per calendar day from 2026-01-01. */
function series(values: number[], flows: Record<number, number> = {}): ValueSeriesPoint[] {
  const out: ValueSeriesPoint[] = []
  let cost = 1000
  for (let i = 0; i < values.length; i++) {
    const d = new Date(Date.UTC(2026, 0, 1 + i)).toISOString().slice(0, 10)
    const flow = flows[i] ?? 0
    cost += flow
    out.push({ date: d, market_value_eur: values[i], cost_basis_eur: cost, external_flow_eur: flow })
  }
  return out
}

describe('rollingMetrics', () => {
  it('is empty until the window fills, then agrees with the cards over the last window', () => {
    const vals = Array.from({ length: 140 }, (_, i) => 1000 * (1 + 0.001 * i + 0.01 * Math.sin(i)))
    const s = series(vals)
    expect(rollingMetrics(s, undefined, 200)).toEqual([])

    const rolling = rollingMetrics(s, undefined, 100)
    expect(rolling.length).toBe(139 - 100 + 1)
    const last = rolling[rolling.length - 1]
    // The card, asked about the same 100 returns, must give the same Sharpe and volatility.
    const lastWindow = s.slice(s.length - 101)
    expect(last.sharpe).toBeCloseTo(sharpeRatio(lastWindow)!, 6)
    expect(last.volatilityPct).toBeCloseTo(annualizedVolatilityPct(lastWindow)!, 6)
    expect(last.beta).toBeNull()      // no benchmark handed in
    expect(last.pairedDays).toBe(0)
  })

  it('reports null rather than 0 for a window below the sample floor', () => {
    const s = series(Array.from({ length: 30 }, (_, i) => 1000 + i))
    const rolling = rollingMetrics(s, undefined, 20)
    expect(rolling.length).toBeGreaterThan(0)
    expect(MIN_WINDOW_RETURNS).toBeGreaterThan(20)
    for (const p of rolling) {
      expect(p.sharpe).toBeNull()
      expect(p.volatilityPct).toBeNull()
    }
  })

  it('measures beta on flow-free paired days and skips the days the portfolio traded', () => {
    const n = 80
    const bench = series(Array.from({ length: n }, (_, i) => 1000 * (1 + 0.02 * Math.sin(i / 3))))
    // Portfolio moves exactly twice the benchmark each day; on day 40 a deposit lands
    // (mid-day, so it earns half the move — which is what the pairing rule skips).
    const flows: Record<number, number> = { 40: 500 }
    const port: number[] = [1000]
    for (let i = 1; i < n; i++) {
      const br = bench[i].market_value_eur / bench[i - 1].market_value_eur - 1
      port.push((port[i - 1] + (flows[i] ?? 0)) * (1 + 2 * br))
    }
    const p = series(port, flows)
    const rolling = rollingMetrics(p, bench, 60)
    const last = rolling[rolling.length - 1]
    expect(last.beta).toBeCloseTo(2, 3)
    expect(last.correlation).toBeCloseTo(1, 3)
    expect(last.pairedDays).toBe(59)  // the deposit day is dropped from the pairing
  })
})

describe('drawdownEpisodes', () => {
  it('finds each fall past the threshold, with depth, duration and recovery', () => {
    // 100 → 80 (−20%) → 100 recovered → 97 (−3%, ignored) → 100 → 90 (−10%, still open)
    const vals = [100, 95, 80, 90, 100, 101, 97, 101, 95, 90]
    const s = series(vals)
    const eps = drawdownEpisodes(s, undefined, 5)
    expect(eps.length).toBe(2)
    expect(eps[0].depthPct).toBeCloseTo(-20, 6)
    expect(eps[0].peakDate).toBe('2026-01-01')
    expect(eps[0].troughDate).toBe('2026-01-03')
    expect(eps[0].recoveredDate).toBe('2026-01-05')
    expect(eps[0].daysToTrough).toBe(2)
    expect(eps[0].daysToRecover).toBe(2)
    expect(eps[0].benchmarkPct).toBeNull()
    expect(eps[1].depthPct).toBeCloseTo((90 / 101 - 1) * 100, 6)
    expect(eps[1].recoveredDate).toBeNull()
    expect(eps[1].daysToRecover).toBeNull()
  })

  it('does not read a deposit as a recovery or a withdrawal as a fall', () => {
    // Flat market: value only moves by flows. No drawdown at all.
    const s = series([100, 100, 600, 600, 300, 300], { 2: 500, 4: -300 })
    expect(drawdownEpisodes(s, undefined, 1)).toEqual([])
  })

  it('carries the benchmark move over the same peak-to-trough dates', () => {
    const s = series([100, 90, 80, 100])
    const bench = series([100, 95, 95, 100])
    const eps = drawdownEpisodes(s, bench, 5)
    expect(eps.length).toBe(1)
    expect(eps[0].benchmarkPct).toBeCloseTo(-5, 6)
  })

  it('walks nothing before the first valued point', () => {
    const s = series([0, 0, 100, 50, 100])
    const eps = drawdownEpisodes(s, undefined, 5)
    expect(eps.length).toBe(1)
    expect(eps[0].peakDate).toBe('2026-01-03')
  })
})
