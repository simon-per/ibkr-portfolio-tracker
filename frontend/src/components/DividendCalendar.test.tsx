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
 * shift is checkable, and the pending badge as visible text rather than a hover.
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
    amount_source: 'latest_payment',
    pending: false,
    ...over,
  }
}

describe('DividendCalendar', () => {
  it('shows the expected pay date and keeps the ex-date beside it', () => {
    render(<DividendCalendar upcoming={[payment()]} colorOf={colorOf} />)
    expect(screen.getByText('22 May')).toBeTruthy()
    expect(screen.getByText('ex 8 May')).toBeTruthy()
  })

  it('says the date is an ex-date when no pay date could be found', () => {
    // `pay_date_source: 'ex_date'` means the row IS the ex-date. Printing "ex 8 May"
    // next to "8 May" would dress a fallback up as a measurement; printing nothing
    // let "8 May" read as the day the cash lands.
    render(
      <DividendCalendar
        upcoming={[payment({ date: '2026-05-08', pay_date_source: 'ex_date' })]}
        colorOf={colorOf}
      />,
    )
    expect(screen.queryByText(/^ex /)).toBeNull()
    expect(screen.getByText('ex-date · pay date unknown')).toBeTruthy()
  })

  it('badges an overdue payment in visible text, not in a tooltip', () => {
    render(
      <DividendCalendar
        upcoming={[payment({ date: '2026-04-20', pending: true })]}
        colorOf={colorOf}
      />,
    )
    // On the row only; the explanation under the list went with the owner's call
    // (2026-10-14).
    expect(screen.getAllByText('payment pending').length).toBe(1)
    expect(screen.queryByText(/has gone ex and the cash has not reached/)).toBeNull()
  })

  it('keeps the pending badge out of the truncated symbol, so a narrow row cannot clip it', () => {
    // jsdom lays nothing out, so this pins the structure that makes it visible at 390px:
    // only the symbol truncates, and the badge is its sibling rather than its child.
    render(
      <DividendCalendar
        upcoming={[payment({
          date: '2026-04-20', ex_date: '2026-04-08', pending: true,
          pay_date_source: 'ex_date', symbol: 'VERYLONGSYMBOL',
        })]}
        colorOf={colorOf}
      />,
    )
    expect(screen.getByText('payment pending').closest('.truncate')).toBeNull()
    expect(screen.getByText('ex-date · pay date unknown').closest('.truncate')).toBeNull()
    expect(screen.getByText('VERYLONGSYMBOL').className).toContain('truncate')
  })

  it('says nothing about pending payments when there are none', () => {
    render(<DividendCalendar upcoming={[payment()]} colorOf={colorOf} />)
    expect(screen.queryByText('payment pending')).toBeNull()
  })

  it('groups by the month the cash lands in, not the month it went ex', () => {
    render(
      <DividendCalendar
        upcoming={[payment({ date: '2026-06-02', ex_date: '2026-05-28' })]}
        colorOf={colorOf}
      />,
    )
    const heading = screen.getByText('Jun 2026')
    expect(heading).toBeTruthy()
    expect(screen.queryByText('May 2026')).toBeNull()
  })

  it('renders nothing at all when there is nothing expected', () => {
    const { container } = render(
      <DividendCalendar upcoming={[]} colorOf={colorOf} />,
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
      />,
    )
    const group = screen.getByText('May 2026').parentElement as HTMLElement
    expect(within(group).getByText(/15/)).toBeTruthy()
  })
})
