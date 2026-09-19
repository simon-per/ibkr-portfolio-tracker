// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import type { DividendPace } from '@/lib/dividendPace'
import { DividendGrowthPace } from './DividendGrowthPace'

afterEach(cleanup)

function pace(over: Partial<DividendPace> = {}): DividendPace {
  return {
    cmgr_pct: 18.33, cagr_pct: 653.5, from_month: '2026-01', to_month: '2026-12',
    from_eur: 23.22, to_eur: 147.86, months: 11,
    includes_forecast: false, coverage_limited: false,
    ...over,
  }
}

const strip = () => screen.getByLabelText('Dividend income growth')

describe('the dividend income growth strip', () => {
  it('names both rates and the windows they were measured between', () => {
    render(<DividendGrowthPace pace={pace()} />)

    expect(strip().textContent).toContain('CMGR')
    expect(strip().textContent).toContain('+18.3%')
    expect(strip().textContent).toContain('/mo')
    expect(strip().textContent).toContain('CAGR')
    expect(strip().textContent).toContain('/yr')
    // The two endpoints, so the figure can be checked against the chart below
    // rather than taken on trust.
    expect(strip().textContent).toContain('Jan 26')
    expect(strip().textContent).toContain('Dec 26')
    expect(strip().textContent).toContain('23.22')
    expect(strip().textContent).toContain('147.86')
  })

  it('drops CAGR when there is under a year of span to annualize from', () => {
    render(<DividendGrowthPace pace={pace({ cagr_pct: null, months: 7 })} />)
    expect(strip().textContent).toContain('CMGR')
    expect(strip().textContent).not.toContain('CAGR')
  })

  it('marks a base the account was still being funded inside, on the surface', () => {
    render(<DividendGrowthPace pace={pace({ coverage_limited: true })} />)
    // The dagger carries the full reason in its title, but a marker whose meaning
    // exists only in a hover is one a touch device never reaches — so the short
    // form is visible text.
    expect(strip().textContent).toContain('†')
    expect(strip().textContent).toContain('from the first window on record')
    expect(strip().querySelector('[aria-label*="still being funded"]')).toBeTruthy()
  })

  it('does not mark a rate whose base is a full window', () => {
    render(<DividendGrowthPace pace={pace()} />)
    expect(strip().textContent).not.toContain('†')
    expect(strip().textContent).not.toContain('first window on record')
  })

  it('marks a rate resting on projected windows, and only then', () => {
    render(<DividendGrowthPace pace={pace({ includes_forecast: true })} />)
    expect(strip().textContent).toContain('est.')
    cleanup()
    render(<DividendGrowthPace pace={pace()} />)
    expect(strip().textContent).not.toContain('est.')
  })

  it('stays two lines of figures, with no explanatory prose', () => {
    // The first version carried two paragraphs and the owner asked for KPIs. The
    // qualifiers live on the chips as `est.` and `†` instead, so this pins that
    // the prose does not creep back.
    render(<DividendGrowthPace pace={pace({ includes_forecast: true, coverage_limited: true })} />)
    expect(strip().textContent!.length).toBeLessThan(170)
    expect(strip().querySelectorAll('p')).toHaveLength(0)
  })

  it('renders nothing rather than a dash when the range has no pace', () => {
    // Absent, not zeroed: "0.0%" on a range with one window reads as "flat",
    // which is an answer it has not earned.
    const { container } = render(<DividendGrowthPace pace={null} />)
    expect(container.innerHTML).toBe('')
  })
})
