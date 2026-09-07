// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { PortfolioValueChart } from './PortfolioValueChart'
import type { BenchmarkDataset } from './PortfolioValueChart'
import type { PortfolioValuePoint } from '@/lib/api'

/**
 * The chart's job here is to say when its own line is not a valuation.
 *
 * A day the backend could not fully price omits the unpriced holdings from market value
 * while keeping their cost, so the line dips for a reason that is not a loss. Measured on
 * production: 14 days past the last cached price the total read a plausible +15%, and 15
 * days past it read −100%. The risk metrics now exclude those pairs, but the line still
 * plots them, so the shape would otherwise speak for itself.
 */

afterEach(cleanup)

// `ResponsiveContainer` measures with a ResizeObserver, absent from jsdom. Same stub as
// the other component specs.
beforeEach(() => {
  if (!('ResizeObserver' in globalThis)) {
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver
  }
})

function point(date: string, mv: number, unpriced?: number): PortfolioValuePoint {
  return {
    date,
    cost_basis_eur: 1000,
    market_value_eur: mv,
    gain_loss_eur: mv - 1000,
    gain_loss_percent: ((mv - 1000) / 1000) * 100,
    external_flow_eur: 0,
    ...(unpriced === undefined ? {} : { unpriced_holdings: unpriced }),
  }
}

const HEALTHY = [
  point('2026-03-02', 1000),
  point('2026-03-03', 1010),
  point('2026-03-04', 1020),
]

function benchmark(anchorDate: string | null, anchorUnpriced = 0, name = 'S&P 500'): BenchmarkDataset {
  return {
    key: name.toLowerCase().replace(/\W+/g, ''),
    name,
    color: '#3b82f6',
    data: HEALTHY.map((p) => ({
      date: p.date, benchmark_value_eur: p.market_value_eur, cost_basis_eur: 1000,
      gain_loss_eur: 0, gain_loss_percent: 0,
    })),
    anchorDate,
    anchorUnpriced,
  }
}

describe('the incomplete-valuation notice', () => {
  it('stays hidden when every day was fully priced', () => {
    render(<PortfolioValueChart data={HEALTHY} />)
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('stays hidden when the backend does not report the field at all', () => {
    // Older than 2026-08-05: absent must read as complete, not as unmeasurable, or every
    // chart would carry a permanent warning.
    const noField = HEALTHY.map(({ unpriced_holdings, ...rest }) => rest as PortfolioValuePoint)
    render(<PortfolioValueChart data={noField} />)
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('names the day count and the worst holding count when a feed stalls', () => {
    render(
      <PortfolioValueChart
        data={[...HEALTHY, point('2026-03-05', 600, 2), point('2026-03-06', 0, 5)]}
      />,
    )
    const alert = screen.getByRole('alert')
    expect(alert.textContent).toMatch(/2 days in this range could not be fully valued/)
    expect(alert.textContent).toMatch(/up to 5 holdings/)
    expect(alert.textContent).toMatch(/2026-03-05/)
  })

  it('says the dip is missing data rather than a loss', () => {
    // The whole point: without this the shape reads as a crash, which is exactly how a
    // stalled sync would be misread.
    render(<PortfolioValueChart data={[...HEALTHY, point('2026-03-05', 0, 5)]} />)
    expect(screen.getByRole('alert').textContent).toMatch(/rather than a loss/)
  })

  it('uses the singular for one day and one holding', () => {
    render(<PortfolioValueChart data={[...HEALTHY, point('2026-03-05', 900, 1)]} />)
    const text = screen.getByRole('alert').textContent ?? ''
    expect(text).toMatch(/1 day in this range/)
    expect(text).toMatch(/up to 1 holding\b/)
  })
})

/**
 * A benchmark line that starts at the portfolio's own value reads as the since-inception
 * comparison it replaced unless the chart says what it is. The sentence is prose rather
 * than a tooltip because a tooltip does not exist on a phone.
 */
describe('the benchmark anchor note', () => {
  it('says where the benchmark line starts, and what it means', async () => {
    render(<PortfolioValueChart data={HEALTHY} benchmarks={[benchmark('2026-03-02')]} />)
    const note = await screen.findByText(/start at your portfolio's value on Mar 2, 2026/)
    expect(note.textContent).toMatch(/moved everything into that index that day/)
    expect(note.textContent).toMatch(/same contributions since/)
  })

  it('is absent when no benchmark is drawn', () => {
    render(<PortfolioValueChart data={HEALTHY} />)
    expect(screen.queryByText(/start at your portfolio's value/)).toBeNull()
  })

  it('is absent for a backend that sends no anchor, rather than rendered wrong', async () => {
    // Older than 2026-09-07 the series is since-inception, and describing it as seeded
    // from the portfolio's value would be the exact misreading the sentence exists to stop.
    render(<PortfolioValueChart data={HEALTHY} benchmarks={[benchmark(null)]} />)
    await screen.findByRole('button', { name: /S&P 500/ })
    expect(screen.queryByText(/start at your portfolio's value/)).toBeNull()
  })

  it('names each benchmark when they anchor on different days', async () => {
    render(
      <PortfolioValueChart
        data={HEALTHY}
        benchmarks={[benchmark('2026-03-02'), benchmark('2026-03-03', 0, 'DAX')]}
      />,
    )
    const note = await screen.findByText(/S&P 500 from Mar 2, 2026, DAX from Mar 3, 2026/)
    expect(note.textContent).toMatch(/first day each index could be priced/)
  })

  it('says the line is understated when the anchor day could not be fully valued', async () => {
    render(
      <PortfolioValueChart
        data={[point('2026-03-02', 600, 2), ...HEALTHY.slice(1)]}
        benchmarks={[benchmark('2026-03-02', 2)]}
      />,
    )
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/1 day in this range could not be fully valued/)
    expect(alert.textContent).toMatch(
      /The S&P 500 benchmark line starts from a day the portfolio could not be fully valued/,
    )
    expect(alert.textContent).toMatch(/understated for the whole range/)
  })
})
