// @vitest-environment jsdom
import { describe, it, expect, afterEach, beforeEach } from 'vitest'
import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import { MonthlyReturnsHeatmap } from './MonthlyReturnsHeatmap'
import type { PortfolioValuePoint } from '@/lib/api'

/**
 * The card is collapsed by default, so its one-line summary is the only thing most
 * readers ever see — and it rendered the figure with no partial marker while the table
 * inside badged the same figure with a dagger and explained it in a footnote.
 *
 * That is how a "YTD" measured over six weeks of a year read as an answer: one unpriceable
 * holding (a spinoff whose tax lots predate its listing) made every day from November to
 * June unmeasurable, `computeModifiedDietzReturn` trimmed the window to the days it could
 * use, and the collapsed line showed +3.1% flat.
 */

afterEach(cleanup)

// `ScrollableTable` measures overflow with a ResizeObserver, which jsdom does not implement.
beforeEach(() => {
  if (!('ResizeObserver' in globalThis)) {
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver
  }
})

function point(date: string, mv: number, unpriced = 0): PortfolioValuePoint {
  return {
    date,
    cost_basis_eur: 900,
    market_value_eur: mv,
    gain_loss_eur: mv - 900,
    gain_loss_percent: 0,
    external_flow_eur: 0,
    unpriced_holdings: unpriced,
  }
}

describe('MonthlyReturnsHeatmap', () => {
  it('keeps the collapsed summary to the figures, even over part of a period', () => {
    // February is fine; March's last day cannot be valued, so the YTD figure and the
    // March figure both stop short of the period they are labelled with.
    render(
      <MonthlyReturnsHeatmap
        isLoading={false}
        data={[
          point('2026-02-02', 1000),
          point('2026-02-27', 1010),
          point('2026-03-02', 1010),
          point('2026-03-30', 1030),
          point('2026-03-31', 400, 2),
        ]}
      />
    )
    // The owner's call (2026-10-14): no qualifier on the surface.
    expect(screen.queryByRole('table')).toBeNull()
    expect(screen.getByText(/Mar:/)).toBeTruthy()
    expect(screen.queryByText(/part of the period only|†/)).toBeNull()
  })

  it('leaves the summary unqualified when every period is complete', () => {
    render(
      <MonthlyReturnsHeatmap
        isLoading={false}
        data={[
          point('2026-03-02', 1000),
          point('2026-03-31', 1050),
        ]}
      />
    )
    expect(screen.queryByText(/part of the period only/)).toBeNull()
    expect(screen.getByText(/Mar:/)).toBeTruthy()
  })

  it('tags the running month MTD, in the summary and in its cell only', () => {
    render(
      <MonthlyReturnsHeatmap
        isLoading={false}
        runningMonth="2026-03"
        data={[point('2026-02-02', 1000), point('2026-02-27', 1010), point('2026-03-31', 1030)]}
      />,
    )
    expect(screen.getByText(/Mar MTD:/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /Monthly Returns/ }))
    expect(screen.getAllByText('MTD')).toHaveLength(1)
    expect(screen.getByTitle(/^Mar 2026:/).textContent).toContain('MTD')
    expect(screen.getByTitle(/^Feb 2026:/).textContent).not.toContain('MTD')
    // An empty month is the app's one absent glyph, not an en dash.
    expect(screen.getAllByText('—').length).toBeGreaterThan(0)
    expect(screen.queryByText('–')).toBeNull()
  })

  it('says the request failed rather than claiming there is not enough data', () => {
    render(<MonthlyReturnsHeatmap isLoading={false} isError data={undefined} />)
    expect(screen.getByText(/Could not load the value history/)).toBeTruthy()
  })
})

describe('a range that begins inside a period', () => {
  /**
   * On 1Y today the range starts 2025-09-12, so last year's row is built from Sep 12 →
   * Dec 31 and labelled "YTD", and its September cell from Sep 12 → 30 and labelled
   * "Sep". Every point is complete, so the unpriced trim saw nothing. `rangeStart` is
   * what lets the component tell; the cell tooltip names the days it covers.
   */
  const oneYearFromSeptember = [
    point('2025-09-12', 1000), point('2025-09-30', 1010),
    point('2025-10-01', 1010), point('2025-10-31', 1020),
    point('2025-12-01', 1020), point('2025-12-31', 1030),
    point('2026-01-02', 1030), point('2026-01-30', 1040),
  ]

  const open = () => fireEvent.click(screen.getByRole('button', { name: /Monthly Returns/ }))
  const cell = (label: RegExp) => screen.getByTitle(label)

  it("names the measured days on last year's YTD and first month, and not on whole periods", () => {
    render(<MonthlyReturnsHeatmap isLoading={false} data={oneYearFromSeptember} rangeStart="2025-09-12" />)
    open()
    expect(cell(/^YTD 2025:/).getAttribute('title')).toContain('measured')
    expect(cell(/^Sep 2025:/).getAttribute('title')).toContain('measured')
    expect(cell(/^Oct 2025:/).getAttribute('title')).not.toContain('measured')
    expect(cell(/^YTD 2026:/).getAttribute('title')).not.toContain('measured')
    expect(cell(/^Jan 2026:/).getAttribute('title')).not.toContain('measured')
    expect(screen.queryByText(/†/)).toBeNull()
  })

  it('names the range as the cause in the tooltip, not a stalled sync', () => {
    render(<MonthlyReturnsHeatmap isLoading={false} data={oneYearFromSeptember} rangeStart="2025-09-12" />)
    open()
    const title = cell(/^YTD 2025:/).getAttribute('title')
    expect(title).toMatch(/measured 2025-09-12 → 2025-12-31; the selected range starts inside this period/)
    expect(title).not.toMatch(/could not be fully valued/)
  })

  it('does not badge a YTD range that begins on 1 January, whatever day trading resumed', () => {
    render(
      <MonthlyReturnsHeatmap
        isLoading={false}
        data={[point('2026-01-02', 1000), point('2026-01-30', 1040)]}
        rangeStart="2026-01-01"
      />,
    )
    open()
    expect(cell(/^YTD 2026:/).getAttribute('title')).not.toContain('measured')
    expect(cell(/^Jan 2026:/).getAttribute('title')).not.toContain('measured')
  })

  it('flags nothing on the range\'s account when the caller passes no range start', () => {
    render(<MonthlyReturnsHeatmap isLoading={false} data={oneYearFromSeptember} />)
    open()
    expect(cell(/^YTD 2025:/).getAttribute('title')).not.toContain('measured')
  })
})
