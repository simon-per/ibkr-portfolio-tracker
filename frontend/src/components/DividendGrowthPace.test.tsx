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
    includes_forecast: false,
    ...over,
  }
}

const strip = () => screen.getByLabelText('Dividend income growth')

describe('the dividend income growth strip', () => {
  it('names both rates in words, not acronyms', () => {
    render(<DividendGrowthPace pace={pace()} />)

    expect(strip().textContent).toContain('+18.3%')
    expect(strip().textContent).toContain('per month')
    expect(strip().textContent).toContain('per year')
    expect(strip().textContent).not.toContain('CMGR')
  })

  it('drops CAGR when there is under a year of span to annualize from', () => {
    render(<DividendGrowthPace pace={pace({ cagr_pct: null, months: 7 })} />)
    expect(strip().textContent).toContain('per month')
    expect(strip().textContent).not.toContain('per year')
  })

  it('marks a rate resting on projected windows, and only then', () => {
    render(<DividendGrowthPace pace={pace({ includes_forecast: true })} />)
    expect(strip().textContent).toContain('est.')
    cleanup()
    render(<DividendGrowthPace pace={pace()} />)
    expect(strip().textContent).not.toContain('est.')
  })

  it('stays two lines of figures, with no explanatory prose', () => {
    // The first version carried two paragraphs and the owner asked for KPIs; the
    // data-quality dagger went on 2026-10-14. This pins that neither creeps back.
    render(<DividendGrowthPace pace={pace({ includes_forecast: true })} />)
    expect(strip().textContent!.length).toBeLessThan(170)
    expect(strip().querySelectorAll('p')).toHaveLength(0)
    expect(strip().textContent).not.toContain('†')
  })

  it('renders nothing rather than a dash when the range has no pace', () => {
    // Absent, not zeroed: "0.0%" on a range with one window reads as "flat",
    // which is an answer it has not earned.
    const { container } = render(<DividendGrowthPace pace={null} />)
    expect(container.innerHTML).toBe('')
  })
})
