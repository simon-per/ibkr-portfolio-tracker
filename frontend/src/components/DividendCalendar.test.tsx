// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen, within } from '@testing-library/react'
import { DividendCalendar } from './DividendCalendar'
import type { DividendUpcomingPayment } from '@/lib/api'

/**
 * The calendar answers "when does the money arrive", and it had no tests at all while it
 * was answering a different question: every date it showed was the **ex-date**, because
 * the projection infers its cadence from yfinance's ex-date series and nothing shifted
 * it. So a row disappeared on the day the dividend went ex and did not return until IBKR
 * posted the cash — up to a month, during which the payment was in no figure anywhere.
 *
 * What is pinned here is what a reader can see: the ex-date beside the pay date so the
 * shift is checkable, the pending badge as visible text rather than a hover, and the
 * caption following the weakest provenance on screen rather than the best.
 */

afterEach(cleanup)

const colorOf = () => '#000000'

function payment(over: Partial<DividendUpcomingPayment> = {}): DividendUpcomingPayment {
  return {
    date: '2026-05-22',
    ex_date: '2026-05-08',
    security_id: 1,
    symbol: 'ASML',
    net_eur: 12.5,
    basis: 'net',
    pay_date_source: 'measured_lag',
    pending: false,
    ...over,
  }
}

describe('DividendCalendar', () => {
  it('labels gross-derived amounts as estimated net with assumed withholding', () => {
    render(
      <DividendCalendar
        upcoming={[payment({ net_eur: 85, basis: 'gross_estimate', pending: true })]}
        colorOf={colorOf}
        withholdingPct={15}
      />,
    )
    expect(screen.getByText(/published gross dividends using 15% assumed withholding/)).toBeTruthy()
    expect(screen.getByTitle('Estimated net from gross — 15% assumed withholding')).toBeTruthy()
    expect(screen.queryByText(/withholding tax is not deducted/)).toBeNull()
  })

  it('shows the expected pay date and keeps the ex-date beside it', () => {
    render(<DividendCalendar upcoming={[payment()]} colorOf={colorOf} withholdingPct={15} />)
    expect(screen.getByText('22 May')).toBeTruthy()
    expect(screen.getByText('ex 8 May')).toBeTruthy()
  })

  it('does not repeat the date as an ex-date when nothing could be measured', () => {
    // `pay_date_source: 'ex_date'` means the row IS the ex-date. Printing "ex 8 May"
    // next to "8 May" would dress a fallback up as a measurement.
    render(
      <DividendCalendar
        upcoming={[payment({ date: '2026-05-08', pay_date_source: 'ex_date' })]}
        colorOf={colorOf}
        withholdingPct={15}
      />,
    )
    expect(screen.queryByText(/^ex /)).toBeNull()
  })

  it('badges an overdue payment in visible text, not in a tooltip', () => {
    render(
      <DividendCalendar
        upcoming={[payment({ date: '2026-04-20', pending: true })]}
        colorOf={colorOf}
        withholdingPct={15}
      />,
    )
    // Twice: once on the row, once in the explanation under the list. A caveat reachable
    // only by hovering does not exist on a touch device.
    expect(screen.getAllByText('payment pending').length).toBe(2)
    expect(screen.getByText(/has gone ex and the cash has not reached/)).toBeTruthy()
  })

  it('says nothing about pending payments when there are none', () => {
    render(<DividendCalendar upcoming={[payment()]} colorOf={colorOf} withholdingPct={15} />)
    expect(screen.queryByText('payment pending')).toBeNull()
  })

  it('claims an announced schedule only when every row is announced', () => {
    const { rerender } = render(
      <DividendCalendar
        upcoming={[payment({ pay_date_source: 'accrual' })]}
        colorOf={colorOf}
        withholdingPct={15}
      />,
    )
    expect(screen.getByText('pay dates as announced by IBKR')).toBeTruthy()

    // One inferred row is enough to drop the claim: the caption follows the weakest
    // provenance on screen, because "announced" over an inferred date is a stronger
    // statement than the data supports.
    rerender(
      <DividendCalendar
        upcoming={[
          payment({ pay_date_source: 'accrual' }),
          payment({ security_id: 2, symbol: 'MCO', pay_date_source: 'measured_lag' }),
        ]}
        colorOf={colorOf}
        withholdingPct={15}
      />,
    )
    expect(screen.queryByText('pay dates as announced by IBKR')).toBeNull()
    expect(screen.getByText(/dated when the cash is expected/)).toBeTruthy()
  })

  it('groups by the month the cash lands in, not the month it went ex', () => {
    render(
      <DividendCalendar
        upcoming={[payment({ date: '2026-06-02', ex_date: '2026-05-28' })]}
        colorOf={colorOf}
        withholdingPct={15}
      />,
    )
    const heading = screen.getByText('Jun 2026')
    expect(heading).toBeTruthy()
    expect(screen.queryByText('May 2026')).toBeNull()
  })

  it('renders nothing at all when there is nothing expected', () => {
    const { container } = render(
      <DividendCalendar upcoming={[]} colorOf={colorOf} withholdingPct={15} />,
    )
    expect(container.firstChild).toBeNull()
  })

  it('totals each month from the rows it shows', () => {
    render(
      <DividendCalendar
        upcoming={[
          payment({ net_eur: 10 }),
          payment({ security_id: 2, symbol: 'MCO', net_eur: 5 }),
        ]}
        colorOf={colorOf}
        withholdingPct={15}
      />,
    )
    const group = screen.getByText('May 2026').parentElement as HTMLElement
    expect(within(group).getByText(/15/)).toBeTruthy()
  })
})
