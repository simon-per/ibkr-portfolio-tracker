import { describe, expect, it } from 'vitest'
import {
  annualizedVolatilityPct,
  benchmarkAsValueSeries,
  betaAndCorrelation,
  concentrationPct,
  dailyReturnSeries,
  dailyReturns,
  drawdownDetail,
  externalFlow,
  herfindahlConcentration,
  isMeasurable,
  maxDrawdownPct,
  sharpeRatio,
  sortinoRatio,
  winRate,
} from './portfolioKpis'
import type { ValueSeriesPoint } from './portfolioKpis'
import type { BenchmarkValuePoint, PortfolioValuePoint } from './api'

function point(
  date: string, mv: number, cost: number, flow?: number,
): PortfolioValuePoint {
  return {
    date,
    market_value_eur: mv,
    cost_basis_eur: cost,
    gain_loss_eur: mv - cost,
    gain_loss_percent: cost ? ((mv - cost) / cost) * 100 : 0,
    ...(flow === undefined ? {} : { external_flow_eur: flow }),
  }
}

describe('externalFlow', () => {
  it('uses the backend flow when present', () => {
    const prev = point('2026-01-01', 1000, 900)
    const curr = point('2026-01-02', 900, 800, -110)
    expect(externalFlow(prev, curr)).toBe(-110)
  })

  it('falls back to the cost-basis delta when the field is absent', () => {
    const prev = point('2026-01-01', 1000, 900)
    const curr = point('2026-01-02', 1100, 1000)
    expect(externalFlow(prev, curr)).toBe(100)
  })
})

describe('dailyReturns', () => {
  it('nets out a purchase so buying is not a gain', () => {
    // Bought 100 at market: value and cost both rise by 100, return is zero.
    const series = [point('2026-01-01', 1000, 1000), point('2026-01-02', 1100, 1100, 100)]
    expect(dailyReturns(series)[0]).toBeCloseTo(0, 10)
  })

  it('nets out a sale at proceeds, not at cost', () => {
    // A position bought for 50 is sold for 200: value drops 200, cost drops 50.
    // The real return that day is zero — nothing was lost, capital left.
    const series = [
      point('2026-01-01', 1000, 500),
      point('2026-01-02', 800, 450, -200),
    ]
    expect(dailyReturns(series)[0]).toBeCloseTo(0, 10)
  })

  it('without the backend flow, that same sale fabricates a loss', () => {
    // The regression this field exists to remove: inferring the flow from the
    // cost-basis line (−50) makes the day look like a 150 loss.
    const series = [
      point('2026-01-01', 1000, 500),
      point('2026-01-02', 800, 450),
    ]
    expect(dailyReturns(series)[0]).toBeLessThan(-0.1)
  })

  it('measures a real price move', () => {
    const series = [point('2026-01-01', 1000, 1000, 0), point('2026-01-02', 1100, 1000, 0)]
    expect(dailyReturns(series)[0]).toBeCloseTo(0.1, 10)
  })

  it('skips days with no value to divide by', () => {
    const series = [point('2026-01-01', 0, 0, 0), point('2026-01-02', 0, 0, 0)]
    expect(dailyReturns(series)).toEqual([])
  })
})

describe('maxDrawdownPct', () => {
  it('is zero for a series that only rises', () => {
    const series = [
      point('2026-01-01', 100, 100, 0),
      point('2026-01-02', 110, 100, 0),
      point('2026-01-03', 120, 100, 0),
    ]
    expect(maxDrawdownPct(series)).toBe(0)
  })

  it('reports the worst peak-to-trough fall, not the last one', () => {
    const series = [
      point('2026-01-01', 100, 100, 0),
      point('2026-01-02', 200, 100, 0),   // peak
      point('2026-01-03', 100, 100, 0),   // −50% from peak
      point('2026-01-04', 180, 100, 0),
      point('2026-01-05', 162, 100, 0),   // −10% from the later high
    ]
    expect(maxDrawdownPct(series)).toBeCloseTo(-50, 6)
  })

  it('a sale is not a drawdown', () => {
    const series = [
      point('2026-01-01', 1000, 500, 0),
      point('2026-01-02', 800, 450, -200),   // sold, did not fall
    ]
    expect(maxDrawdownPct(series)).toBeCloseTo(0, 6)
  })
})

describe('sharpeRatio', () => {
  it('is null, never 0, below the minimum sample', () => {
    // It used to return 0, and MTD in the first days of a month reaches that in one
    // click — 2 daily returns, rendered as a green `0.00` captioned "Risk-adjusted
    // return" beside a dashed Volatility and Sortino. `0.00` is a plausible Sharpe, so
    // nothing about the number invited doubt.
    const series = [point('2026-01-01', 100, 100, 0), point('2026-01-02', 101, 100, 0)]
    expect(sharpeRatio(series)).toBeNull()
  })

  it('is null when there is no volatility to divide by', () => {
    // A perfectly flat series is what a stale price feed looks like, and return per unit
    // of risk is undefined when there is no risk.
    const flat = Array.from({ length: 10 }, (_, i) =>
      point(`2026-01-${String(i + 1).padStart(2, '0')}`, 100, 100, 0))
    expect(sharpeRatio(flat)).toBeNull()
  })

  it('is positive for steady gains and negative for steady losses', () => {
    const rising = [100, 101, 102, 103, 104, 105, 106].map((v, i) =>
      point(`2026-01-0${i + 1}`, v, 100, 0))
    const falling = [100, 99, 98, 97, 96, 95, 94].map((v, i) =>
      point(`2026-01-0${i + 1}`, v, 100, 0))
    expect(sharpeRatio(rising)!).toBeGreaterThan(0)
    expect(sharpeRatio(falling)!).toBeLessThan(0)
  })

  it('is clamped rather than exploding on a high return with small volatility', () => {
    // This test used to pass without reaching the clamp. Its old series (+0.5/day on 100)
    // has an annualised σ under 0.001, so it took the negligible-volatility early return
    // — which was `return 0` — and then asserted `|0| <= 10`, true of anything. Now that
    // branch returns null and the vacuity was visible.
    //
    // ~2% a day with a σ of 0.0005 gives a raw ratio in the hundreds, so the clamp is
    // what the assertion actually measures.
    const returns = Array.from({ length: 12 }, (_, i) => (i % 2 === 0 ? 0.02 : 0.021))
    const ratio = sharpeRatio(compounded(returns))
    expect(ratio).not.toBeNull()
    expect(ratio).toBe(10)
  })
})

/** `2026-03-01` upward, so a 30-day series has real ISO dates. */
function day(i: number): string {
  return `2026-03-${String(i + 1).padStart(2, '0')}`
}

/**
 * A flow-free series whose daily returns are exactly the ones asked for, built
 * by compounding so no rounding creeps into the assertions.
 */
function compounded(returns: number[], start = 1000): PortfolioValuePoint[] {
  const out = [point(day(0), start, start, 0)]
  let v = start
  returns.forEach((r, i) => {
    v *= 1 + r
    out.push(point(day(i + 1), v, start, 0))
  })
  return out
}

/** ±1% on alternate days: a mean of zero and a daily σ of exactly 0.01. */
function alternating(count: number, magnitude: number): number[] {
  return Array.from({ length: count }, (_, i) => (i % 2 === 0 ? magnitude : -magnitude))
}

describe('dailyReturnSeries', () => {
  it('tags each return with the day it lands on', () => {
    const series = compounded([0.1, -0.1])
    expect(dailyReturnSeries(series).map((r) => r.date)).toEqual([day(1), day(2)])
  })

  it('keeps dates aligned when a day is dropped for having nothing to divide by', () => {
    // Nothing was held over the first pair, so it is skipped and the surviving
    // return belongs to day 2 — indexing the input by position would have named
    // day 1.
    const series = [
      point(day(0), 0, 0, 0),
      point(day(1), 0, 0, 0),
      point(day(2), 100, 100, 100),
    ]
    const returns = dailyReturnSeries(series)
    expect(returns).toHaveLength(1)
    expect(returns[0].date).toBe(day(2))
  })

  it('agrees with dailyReturns', () => {
    const series = compounded([0.02, -0.01, 0.03])
    expect(dailyReturnSeries(series).map((r) => r.ret)).toEqual(dailyReturns(series))
  })
})

describe('annualizedVolatilityPct', () => {
  it('annualises the daily standard deviation', () => {
    // σ_daily = 0.01 exactly → 0.01 × √252 × 100.
    const vol = annualizedVolatilityPct(compounded(alternating(20, 0.01)))
    expect(vol).toBeCloseTo(Math.sqrt(252), 6)
  })

  it('is null rather than zero below the minimum sample', () => {
    expect(annualizedVolatilityPct(compounded([0.01, 0.01]))).toBeNull()
  })

  it('is zero for a series that never moves', () => {
    expect(annualizedVolatilityPct(compounded(Array(10).fill(0)))).toBeCloseTo(0, 10)
  })
})

describe('sortinoRatio', () => {
  it('is null when nothing fell below the threshold', () => {
    // No downside deviation means an undefined ratio, not a large one.
    expect(sortinoRatio(compounded(Array(10).fill(0.01)))).toBeNull()
  })

  it('is negative for a series that mostly falls', () => {
    const ratio = sortinoRatio(compounded(Array(10).fill(-0.01)))
    expect(ratio).not.toBeNull()
    expect(ratio!).toBeLessThan(0)
  })

  it('rewards the same return achieved with less downside', () => {
    // Both end up ahead; the first gets there without a single down day.
    const smooth = sortinoRatio(compounded([0.01, 0.01, 0.01, 0.01, 0.01, 0.0]))
    const choppy = sortinoRatio(compounded([0.06, -0.04, 0.05, -0.03, 0.04, -0.02]))
    expect(choppy).not.toBeNull()
    // `smooth` has no downside at all, so it is refused rather than ranked —
    // which is the point of returning null.
    expect(smooth).toBeNull()
    expect(choppy!).toBeLessThan(10)
  })

  it('is null below the minimum sample', () => {
    expect(sortinoRatio(compounded([-0.01, -0.01]))).toBeNull()
  })
})

describe('drawdownDetail', () => {
  it('names the peak and the trough of the worst fall', () => {
    const series = compounded([0.5, -0.5, 0.2])
    const detail = drawdownDetail(series)
    expect(detail.maxDrawdownPct).toBeCloseTo(-50, 6)
    expect(detail.peakDate).toBe(day(1))
    expect(detail.troughDate).toBe(day(2))
  })

  it('reports no recovery while the trough is still underwater', () => {
    const detail = drawdownDetail(compounded([0.5, -0.5, 0.2]))
    expect(detail.recoveredDate).toBeNull()
    expect(detail.currentDrawdownPct).toBeLessThan(0)
  })

  it('dates the recovery when a new high is reached', () => {
    // Down 50%, then back above the old peak on the last day.
    const detail = drawdownDetail(compounded([0.5, -0.5, 1.5]))
    expect(detail.recoveredDate).toBe(day(3))
    expect(detail.currentDrawdownPct).toBeCloseTo(0, 6)
  })

  it('separates the worst drawdown from the current one', () => {
    // −50% early and fully recovered, then a shallow −10% that is still open.
    const detail = drawdownDetail(compounded([1.0, -0.5, 1.0, -0.1]))
    expect(detail.maxDrawdownPct).toBeCloseTo(-50, 6)
    expect(detail.currentDrawdownPct).toBeCloseTo(-10, 6)
  })

  it('agrees with maxDrawdownPct', () => {
    const series = compounded([0.3, -0.2, 0.1, -0.35, 0.4])
    const standalone = maxDrawdownPct(series)
    expect(standalone).not.toBeNull()
    expect(drawdownDetail(series).maxDrawdownPct).toBeCloseTo(standalone!, 10)
  })

  it('has no peak or trough to name when nothing ever fell', () => {
    const detail = drawdownDetail(compounded([0.1, 0.1, 0.1]))
    expect(detail.maxDrawdownPct).toBe(0)
    expect(detail.peakDate).toBeNull()
    expect(detail.troughDate).toBeNull()
  })
})

describe('herfindahlConcentration', () => {
  it('reports four equal holdings as an effective four', () => {
    const positions = [25, 25, 25, 25].map((v) => ({ market_value_eur: v }))
    const { hhi, effectiveHoldings } = herfindahlConcentration(positions)
    expect(hhi).toBeCloseTo(0.25, 10)
    expect(effectiveHoldings).toBeCloseTo(4, 10)
  })

  it('sees through a long tail that a top-5 weight cannot', () => {
    // One position at half the book plus nineteen tiny ones. Effective count is
    // nowhere near twenty.
    const positions = [{ market_value_eur: 50 }].concat(
      Array.from({ length: 19 }, () => ({ market_value_eur: 50 / 19 })),
    )
    expect(herfindahlConcentration(positions).effectiveHoldings!).toBeLessThan(4)
  })

  it('has no effective count for an empty or unpriced portfolio', () => {
    expect(herfindahlConcentration([]).effectiveHoldings).toBeNull()
    expect(
      herfindahlConcentration([{ market_value_eur: 0 }]).effectiveHoldings,
    ).toBeNull()
  })

  it('ignores an unpriced position instead of counting it as a small one', () => {
    const priced = [{ market_value_eur: 50 }, { market_value_eur: 50 }]
    const withHole = [...priced, { market_value_eur: 0 }]
    expect(herfindahlConcentration(withHole).effectiveHoldings).toBeCloseTo(
      herfindahlConcentration(priced).effectiveHoldings!,
      10,
    )
  })
})

describe('benchmarkAsValueSeries', () => {
  it('maps the benchmark value onto the shared field', () => {
    const points: BenchmarkValuePoint[] = [
      {
        date: day(0),
        benchmark_value_eur: 1234,
        cost_basis_eur: 1000,
        gain_loss_eur: 234,
        gain_loss_percent: 23.4,
      },
    ]
    expect(benchmarkAsValueSeries(points)).toEqual([
      { date: day(0), market_value_eur: 1234, cost_basis_eur: 1000 },
    ])
  })
})

describe('betaAndCorrelation', () => {
  /**
   * A real benchmark series carries no `external_flow_eur` — that is the whole
   * reason flow days are dropped rather than netted — so the fixture must not
   * hand one out either.
   */
  function withoutFlowField(series: PortfolioValuePoint[]): ValueSeriesPoint[] {
    return series.map(({ date, market_value_eur, cost_basis_eur }) => ({
      date,
      market_value_eur,
      cost_basis_eur,
    }))
  }

  /** Portfolio moving exactly twice the benchmark, in step: beta 2, r = 1. */
  function twiceTheBenchmark(days: number) {
    const bench = withoutFlowField(compounded(alternating(days, 0.01)))
    const port = compounded(alternating(days, 0.02))
    return { port, bench }
  }

  it('recovers a beta of two and a correlation of one', () => {
    const { port, bench } = twiceTheBenchmark(28)
    const { beta, correlation, sampleDays } = betaAndCorrelation(port, bench)
    expect(beta).toBeCloseTo(2, 6)
    expect(correlation).toBeCloseTo(1, 6)
    expect(sampleDays).toBe(28)
  })

  it('is negative when the portfolio moves against the benchmark', () => {
    const bench = withoutFlowField(compounded(alternating(28, 0.01)))
    const port = compounded(alternating(28, -0.01))
    expect(betaAndCorrelation(port, bench).beta!).toBeCloseTo(-1, 6)
  })

  it('refuses a slope drawn from too few days, but still reports the count', () => {
    const { port, bench } = twiceTheBenchmark(10)
    const result = betaAndCorrelation(port, bench)
    expect(result.beta).toBeNull()
    expect(result.correlation).toBeNull()
    expect(result.sampleDays).toBe(10)
  })

  it('drops a day the portfolio saw a flow on', () => {
    const { port, bench } = twiceTheBenchmark(28)
    const flowed = port.map((p, i) => (i === 14 ? { ...p, external_flow_eur: 500 } : p))
    expect(betaAndCorrelation(flowed, bench).sampleDays).toBe(27)
  })

  it('drops a day the portfolio closed a lot on for nothing', () => {
    // A disposal whose proceeds netted to zero leaves `external_flow_eur` at
    // zero while the cost-basis line still steps down — the one flow the
    // authoritative field cannot see, which is why its step is tested too.
    const { port, bench } = twiceTheBenchmark(28)
    const flowed = port.map((p, i) =>
      i >= 14 ? { ...p, cost_basis_eur: p.cost_basis_eur - 500 } : p,
    )
    expect(betaAndCorrelation(flowed, bench).sampleDays).toBe(27)
  })

  it('keeps a day the benchmark cost line moved on but the portfolio did not', () => {
    // Under a non-EUR base the backend converts the benchmark's running cost
    // basis at each point's date, so the line moves with the exchange rate on
    // a day nothing flowed. Reading that as a flow is what left production
    // with 9 usable days out of 147.
    const { port, bench } = twiceTheBenchmark(28)
    const reprojected = bench.map((p, i) => ({
      ...p,
      cost_basis_eur: p.cost_basis_eur * (1 + 0.003 * i),
    }))
    expect(betaAndCorrelation(port, reprojected).sampleDays).toBe(28)
  })

  it('ignores dates the benchmark has no point for', () => {
    const { port, bench } = twiceTheBenchmark(28)
    const gapped = bench.filter((p) => p.date !== day(14))
    // The missing day breaks two pairs: the one ending on it and the next.
    expect(betaAndCorrelation(port, gapped).sampleDays).toBe(26)
  })

  it('has no slope to report against a benchmark that never moved', () => {
    const port = compounded(alternating(28, 0.02))
    const flat = withoutFlowField(compounded(Array(28).fill(0)))
    const { beta, correlation } = betaAndCorrelation(port, flat)
    expect(beta).toBeNull()
    expect(correlation).toBeNull()
  })

  it('is unaffected by an unrelated date ordering in the benchmark', () => {
    const { port, bench } = twiceTheBenchmark(28)
    const shuffled = [...bench].reverse()
    expect(betaAndCorrelation(port, shuffled).beta!).toBeCloseTo(2, 6)
  })
})

describe('concentrationPct', () => {
  it('sums the largest N by value', () => {
    const positions = [10, 50, 20, 5, 5, 10].map((v) => ({ market_value_eur: v }))
    // top 5 of 100 total = 95
    expect(concentrationPct(positions)).toBeCloseTo(95, 6)
  })

  it('is 100% when there are fewer positions than N', () => {
    expect(concentrationPct([{ market_value_eur: 7 }])).toBeCloseTo(100, 6)
  })

  it('handles an empty portfolio', () => {
    // null, not 0: the card tones anything under 50% as good news, so a zero here is a
    // green all-clear drawn from no data at all.
    expect(concentrationPct([])).toBeNull()
    expect(concentrationPct([{ market_value_eur: 0 }])).toBeNull()
  })
})

describe('incomplete valuations are not measured', () => {
  /**
   * A day the backend could not fully price omits the unpriced holdings from market value
   * while keeping their cost, so pairing it with a neighbour invents a move. Complete
   * 1000 followed by fully-unpriced 0 reads as −100% in one day — which then becomes a
   * real max drawdown, a real volatility and a real Sharpe. That is what a stalled
   * market-data sync looks like: not a gap in the line but a decay to zero, with the
   * whole risk row moving to match.
   */
  const stalled: PortfolioValuePoint[] = [
    { ...point(day(0), 1000, 1000, 0) },
    { ...point(day(1), 1010, 1000, 0) },
    { ...point(day(2), 1020, 1000, 0) },
    { ...point(day(3), 1030, 1000, 0) },
    { ...point(day(4), 1040, 1000, 0) },
    { ...point(day(5), 1050, 1000, 0) },
    // The feed stops: value collapses because holdings could not be priced, not because
    // they fell.
    { ...point(day(6), 600, 1000, 0), unpriced_holdings: 2 },
    { ...point(day(7), 0, 1000, 0), unpriced_holdings: 5 },
  ]

  it('drops a pair with an incomplete end instead of booking a −100% day', () => {
    const returns = dailyReturns(stalled)
    // Five real day-over-day moves survive; the two involving unpriced days do not.
    expect(returns).toHaveLength(5)
    expect(Math.min(...returns)).toBeGreaterThan(0)
  })

  it('keeps a stalled feed out of the drawdown', () => {
    expect(maxDrawdownPct(stalled)).toBe(0)
    expect(drawdownDetail(stalled).currentDrawdownPct).toBeCloseTo(0, 6)
  })

  it('keeps a stalled feed out of volatility and Sharpe', () => {
    // Without the guard the −100% day dominates both.
    const vol = annualizedVolatilityPct(stalled)
    expect(vol).not.toBeNull()
    expect(vol!).toBeLessThan(5)
    expect(sharpeRatio(stalled)).not.toBeNull()
  })

  it('excludes an unpriced day from beta as well', () => {
    // betaAndCorrelation derives its own returns, so it needs the same guard — the loop
    // that pairs against the benchmark would otherwise regress a fabricated −100%.
    const bench = stalled.map((p) => ({ date: p.date, value_eur: 1000, cost_basis_eur: 1000 }))
    const { sampleDays } = betaAndCorrelation(stalled, benchmarkAsValueSeries(bench as never))
    expect(sampleDays).toBe(5)
  })

  it('treats an absent field as complete, so an older backend still measures', () => {
    // The field only exists from 2026-08-05; absent must not mean "unmeasurable".
    const noField = [point(day(0), 1000, 1000, 0), point(day(1), 1100, 1000, 0)]
    expect(dailyReturns(noField)).toHaveLength(1)
    expect(isMeasurable(noField[0])).toBe(true)
  })

  it('refuses a drawdown outright when NOTHING in the range is measurable', () => {
    /**
     * The window the guard above could not save: every point unpriced, so every pair is
     * dropped and there is not one return to walk. `0` there is not an absence, it is a
     * claim — `RiskMetricsCards` reads a zero max as licence to print "Never below its
     * opening value", in green, which is the worst possible answer for a stalled feed.
     * Reachable on a 7D range during a multi-day sync outage, or on MTD in the first days
     * of a month over a freshly bought holding with no price yet — the same reachability
     * that got Sharpe fixed.
     */
    const blind: PortfolioValuePoint[] = [
      { ...point(day(0), 600, 1000, 0), unpriced_holdings: 2 },
      { ...point(day(1), 400, 1000, 0), unpriced_holdings: 3 },
      { ...point(day(2), 0, 1000, 0), unpriced_holdings: 5 },
    ]
    expect(dailyReturns(blind)).toHaveLength(0)
    expect(maxDrawdownPct(blind)).toBeNull()

    const detail = drawdownDetail(blind)
    expect(detail.maxDrawdownPct).toBeNull()
    expect(detail.currentDrawdownPct).toBeNull()
    // The count says how thin the window was, the way betaAndCorrelation's does.
    expect(detail.sampleDays).toBe(0)
  })

  it('still reports a measured zero as zero, which is a different statement', () => {
    // A portfolio that genuinely only ever rose. "Never fell" is true here and must stay
    // sayable — the fix must not turn every flat window into an absence.
    const rising = compounded([0.1, 0.1, 0.1])
    expect(maxDrawdownPct(rising)).toBe(0)
    expect(drawdownDetail(rising).maxDrawdownPct).toBe(0)
    expect(drawdownDetail(rising).sampleDays).toBe(3)
  })
})

describe('winRate', () => {
  const pos = (mv: number, cost: number, price: number | null = 1) => ({
    market_price: price,
    market_value_eur: mv,
    gain_loss_eur: mv - cost,
  })

  it('is the share of judged positions sitting at a gain', () => {
    const positions = [pos(120, 100), pos(90, 100), pos(150, 100), pos(80, 100)]
    const { pct, profitable, judged, excluded } = winRate(positions)
    expect(pct).toBeCloseTo(50, 6)
    expect([profitable, judged, excluded]).toEqual([2, 4, 0])
  })

  it('does not count an unpriced holding as a loss', () => {
    /**
     * The bug: `get_positions_breakdown` values an unpriceable holding at 0.00, so its
     * gain is −cost — a large negative. It therefore left the numerator and stayed in the
     * denominator, and the card's footnote stated it as fact ("3 of 4 profitable"). Three
     * newly bought ETFs sat in exactly this state for hours on 2026-08-07.
     */
    const positions = [pos(120, 100), pos(150, 100), pos(130, 100), pos(0, 100, null)]
    const { pct, profitable, judged, excluded } = winRate(positions)
    expect(pct).toBeCloseTo(100, 6)      // was 75 — the unpriced row read as a loser
    expect([profitable, judged, excluded]).toEqual([3, 3, 1])
  })

  it('catches the FX-rate failure, where market_price survives and the value does not', () => {
    // The narrower `market_price === null` predicate misses this: the backend zeroes the
    // value when it has no rate for the price currency and leaves the price populated.
    // Frankfurter cannot serve TWD at all, which is why WARM_CURRENCIES exists.
    const { pct, judged, excluded } = winRate([pos(120, 100), pos(0, 100, 4.79)])
    expect(pct).toBeCloseTo(100, 6)
    expect([judged, excluded]).toEqual([1, 1])
  })

  it('is null, never 0, when nothing can be valued', () => {
    // A 0% win rate is a plausible figure, so nothing about it invites doubt — the same
    // reasoning as concentrationPct, whose zero was worse still for being reassuring.
    expect(winRate([pos(0, 100, null), pos(0, 200, null)]).pct).toBeNull()
    expect(winRate([]).pct).toBeNull()
  })
})
