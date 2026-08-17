// @vitest-environment jsdom
import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { RiskMetricsCards, type RiskMetrics } from './RiskMetricsCards'
import type { DividendForwardYield } from '@/lib/api'

/**
 * Every metric on this row can legitimately be unknown, and each has its own
 * reason. The failure mode these pin is the one the rest of this codebase keeps
 * paying for: an unknown rendered as a number — a `0` volatility for "not
 * enough history", or a beta drawn from a fortnight shown with no qualifier.
 */

afterEach(cleanup)

const base: RiskMetrics = {
  volatilityPct: 18.4,
  sortino: 1.35,
  currentDrawdownPct: -3.21,
  maxDrawdownPct: -12.5,
  troughDate: '2026-04-07',
  recoveredDate: null,
  beta: { beta: 1.08, correlation: 0.87, sampleDays: 64, minSampleDays: 20, benchmarkName: 'S&P 500' },
}

const dividend: DividendForwardYield = {
  annual_eur: 92.83,
  pct: 0.14,
  on_cost_pct: 0.19,
  paying_holdings: 19,
  priced_holdings: 36,
  unpriced_holdings: 0,
  gross_estimate_eur: 0,
  basis: 'net',
}

/**
 * No `CurrencyProvider` here on purpose: the context's own default is EUR
 * (`CurrencyContext.tsx`), so `useFormatCurrency` works unwrapped and no query fires.
 * That means these assertions must not depend on the currency label — the account runs
 * on CHF and the fixture would render `€`.
 */
function renderWith(
  overrides: Partial<RiskMetrics> = {},
  props: { dividend?: DividendForwardYield | null; dividendError?: boolean } = {},
) {
  return render(<RiskMetricsCards metrics={{ ...base, ...overrides }} {...props} />)
}

describe('RiskMetricsCards', () => {
  it('renders every metric when all of them are known', () => {
    renderWith({}, { dividend })
    expect(screen.getByText('18.4%')).toBeTruthy()
    expect(screen.getByText('1.35')).toBeTruthy()
    expect(screen.getByText('1.08')).toBeTruthy()
    expect(screen.getByText('-3.21%')).toBeTruthy()
    expect(screen.getByText('0.14%')).toBeTruthy()
    expect(screen.getByText('0.19%')).toBeTruthy()
  })

  it('renders nothing at all rather than an empty shell with no metrics', () => {
    const { container } = render(<RiskMetricsCards metrics={null} />)
    expect(container.firstChild).toBeNull()
  })

  it('shows a dash, never a zero, for an unknown volatility', () => {
    renderWith({ volatilityPct: null })
    expect(screen.getByText('Not enough history in this range')).toBeTruthy()
    expect(screen.queryByText('0.0%')).toBeNull()
  })

  it('says why Sortino is absent instead of showing a cap', () => {
    renderWith({ sortino: null })
    expect(screen.getByText('No down days to measure against')).toBeTruthy()
  })

  it('asks for a benchmark when none is selected', () => {
    renderWith({ beta: null })
    expect(screen.getByText('Pick a benchmark to compare against')).toBeTruthy()
  })

  it('names the sample shortfall rather than showing a thin beta', () => {
    renderWith({
      beta: { beta: null, correlation: null, sampleDays: 12, minSampleDays: 20, benchmarkName: 'S&P 500' },
    })
    expect(screen.getByText('Needs 20 flow-free days (12 so far)')).toBeTruthy()
  })

  it('credits the benchmark and the correlation beside a beta', () => {
    renderWith()
    expect(screen.getByText('vs S&P 500 · r 0.87')).toBeTruthy()
  })

  it('drops the correlation from the footnote when only beta resolved', () => {
    renderWith({
      beta: { beta: 1.08, correlation: null, sampleDays: 64, minSampleDays: 20, benchmarkName: 'MSCI World' },
    })
    expect(screen.getByText('vs MSCI World')).toBeTruthy()
  })

  it('dates the trough while the drawdown is still open', () => {
    renderWith()
    expect(screen.getByText('Worst -12.5% on 7 Apr')).toBeTruthy()
  })

  it('says it recovered instead of leaving the worst fall reading as live', () => {
    renderWith({ currentDrawdownPct: 0, recoveredDate: '2026-05-19' })
    expect(screen.getByText('Recovered 19 May from -12.5%')).toBeTruthy()
  })

  it('does not invent a drawdown for a portfolio that never fell', () => {
    renderWith({ currentDrawdownPct: 0, maxDrawdownPct: 0, troughDate: null })
    expect(screen.getByText('Never below its opening value')).toBeTruthy()
  })

  it('does not claim the portfolio never fell over a range it could not measure', () => {
    /**
     * The worst instance of the zero-for-unknown pattern in this app: `maxDrawdownPct`
     * returned 0 when not one daily return in the range was measurable, and this card
     * read that zero as licence to print a positive claim in prose — in green, because
     * the current drawdown was 0 too. A stalled price feed is exactly what produces it,
     * so the reassurance arrived precisely when it was least true.
     */
    renderWith({ currentDrawdownPct: null, maxDrawdownPct: null, troughDate: null })
    expect(screen.queryByText('Never below its opening value')).toBeNull()
    expect(screen.queryByText('0.00%')).toBeNull()
    expect(screen.getAllByText('Not enough history in this range').length).toBeGreaterThan(0)
  })

  it('shows six loading placeholders, matching the grid it fills', () => {
    const { container } = render(<RiskMetricsCards metrics={null} isLoading />)
    expect(container.querySelectorAll('.animate-pulse')).toHaveLength(6)
  })
})

/**
 * The two dividend cards come from a query rather than from `portfolioKpis.ts`, so they
 * have a loading and a failure state the other four do not. Everything here is about
 * keeping those distinguishable — and about the row not becoming hostage to them.
 */
describe('the dividend rate cards', () => {
  it('states the annual amount and the coverage beside the rate', () => {
    renderWith({}, { dividend })
    expect(screen.getByText(/19 of 36 pay/)).toBeTruthy()
    expect(screen.getByText('Same income over what it cost')).toBeTruthy()
  })

  it('keeps rendering the other four metrics when the dividend query is still out', () => {
    // The reason `isLoading` excludes the dividend query: volatility, Sortino, beta and
    // drawdown are already computed, and hiding them behind a third request would make
    // this row as slow as its slowest input.
    renderWith({}, { dividend: undefined })
    expect(screen.getByText('18.4%')).toBeTruthy()
    expect(screen.getByText('1.08')).toBeTruthy()
    expect(screen.getByText('-3.21%')).toBeTruthy()
  })

  it('states a failure rather than showing a bare dash', () => {
    // Absent data must never read as a computed answer — the rule the rebalance panel
    // shipped the bug for.
    renderWith({}, { dividend: null, dividendError: true })
    expect(screen.getAllByText("Couldn't load dividend data")).toHaveLength(2)
  })

  it('tells "nothing projects" apart from "we could not ask"', () => {
    renderWith({}, { dividend: null })
    expect(screen.getAllByText('No projected dividends')).toHaveLength(2)
    expect(screen.queryByText("Couldn't load dividend data")).toBeNull()
  })

  it('never renders an absent rate as a zero', () => {
    renderWith({}, { dividend: null })
    expect(screen.queryByText('0.00%')).toBeNull()
  })

  it('marks a rate whose projection is partly a gross estimate', () => {
    // 7% of this account's projection deducts no withholding, so a bare "projected"
    // would claim a net figure it is not. In the footnote, not only in the tooltip:
    // a caveat reachable by hovering does not exist on a phone.
    renderWith({}, { dividend: { ...dividend, basis: 'mixed', gross_estimate_eur: 6.61 } })
    expect(screen.getByText(/projected, part gross/)).toBeTruthy()
  })

  it('names unpriced holdings, which are excluded from both sides', () => {
    renderWith({}, { dividend: { ...dividend, unpriced_holdings: 1 } })
    expect(screen.getByText(/1 unpriced/)).toBeTruthy()
  })

  it('shows the rate but not a yield on cost when only the cost basis is missing', () => {
    renderWith({}, { dividend: { ...dividend, on_cost_pct: null } })
    expect(screen.getByText('0.14%')).toBeTruthy()
    expect(screen.getByText('No projected dividends')).toBeTruthy()
  })
})

describe('a backend failure is stated, not silent', () => {
  /**
   * The rule `RebalanceCard` and `CurrencyExposureCard` already followed and this row did
   * not: returning null on an error makes an outage look like a feature that was never
   * there. `e2e/errors.mjs` asserts every surface says so with the backend stopped, and
   * this row was not among them.
   */
  it('says the backend did not respond instead of rendering nothing', () => {
    const { container } = render(<RiskMetricsCards metrics={null} isError />)
    expect(container.firstChild).not.toBeNull()
    expect(screen.getByText(/backend didn't respond/)).toBeTruthy()
  })

  it('names what is unavailable rather than failing generically', () => {
    render(<RiskMetricsCards metrics={null} isError />)
    expect(screen.getByText(/Volatility, Sortino, beta, drawdown/)).toBeTruthy()
  })

  it('still renders nothing when there is simply no data yet', () => {
    // An empty portfolio is not a failure, and must not claim one.
    const { container } = render(<RiskMetricsCards metrics={null} />)
    expect(container.firstChild).toBeNull()
  })

  it('prefers the error state over the loading skeleton it replaces', () => {
    const { container } = render(<RiskMetricsCards metrics={null} isError />)
    expect(container.querySelectorAll('.animate-pulse')).toHaveLength(0)
  })
})
