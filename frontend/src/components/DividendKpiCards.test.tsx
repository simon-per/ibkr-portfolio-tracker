// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import type { DividendGrowth } from '@/lib/api'
import { DividendKpiCards } from './DividendKpiCards'

afterEach(cleanup)

function growth(extra: Partial<DividendGrowth> = {}): DividendGrowth {
  return {
    ttm: { net_eur: 130, prev_net_eur: 100, pct: 30 },
    ytd: { net_eur: 30, prev_net_eur: 20, pct: 50 },
    avg_month: { net_eur: 10.83, prev_net_eur: 8.33, pct: 30 },
    ttm_crosses_era: false, next_12m_eur: 1234.5, next_12m_vs_ttm_pct: 23.1,
    annual: [], latest_month: null, ytd_pace: null, recent_pace: null,
    ...extra,
  }
}

const tile = (label: string) => screen.getByText(label).parentElement!

describe('DividendKpiCards', () => {
  it('compares against the prior period when there is one', () => {
    render(<DividendKpiCards growth={growth()} />)
    expect(tile('Last 12 months').textContent).toContain('vs prior 12M')
    expect(tile('Last 12 months').getAttribute('title')).toContain('€100.00')
  })

  it('shows no comparison at all without a prior period, never one against 0.00', () => {
    render(<DividendKpiCards growth={growth({
      ttm: { net_eur: 130, prev_net_eur: null, pct: null },
      ytd: { net_eur: 30, prev_net_eur: null, pct: null },
      avg_month: { net_eur: 10.83, prev_net_eur: null, pct: null },
    })} />)
    for (const label of ['Last 12 months', 'Average per month']) {
      expect(tile(label).textContent).not.toContain('vs prior 12M')
      expect(tile(label).getAttribute('title')).not.toContain('0.00')
    }
    expect(tile(`${new Date().getFullYear()} so far`).textContent).not.toContain('to date')
  })

  it('drops the cents once a figure reaches four digits', () => {
    render(<DividendKpiCards growth={growth()} />)
    expect(tile('Next 12 months').textContent).toContain('€1,235')
    expect(tile('Last 12 months').textContent).toContain('€130.00')
  })
})
