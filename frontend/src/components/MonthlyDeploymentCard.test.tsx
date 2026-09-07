// @vitest-environment jsdom
import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import { MonthlyDeploymentCard } from './MonthlyDeploymentCard'
import type { ContributionsResponse, ContributionWindow, ContributionMonthlyItem } from '@/lib/api'

/**
 * Two things this card has to get right, both of which it got wrong once.
 *
 * **The 12M average comes from the server.** It sits on the same tab as
 * `ContributionsStrip`, and used to recompute the same quantity from `monthly` — one
 * name, two computations, one screen. The recomputation was wrong in a way its own
 * shape hid: `monthly` only contains months that had activity, so `slice(-12)` takes
 * the last twelve *rows* (which can span more than twelve months) and divides by that
 * row count rather than by the months covered. Both errors push the average up.
 *
 * **The card publishes money in, and only money in.** Until 2026-09-06 it drew the gross
 * deployment alone, which counts a rotation twice by design — so the August 2026
 * Ireland→US ETF switch drew a ~31k bar in a month with a few hundred francs of new
 * money, with nothing beside it to read it against. Deployed was then a second bar for a
 * day and came out on 09-07: it is the *identical* number in 20 of this account's 29
 * months, and the two where it is not are what the rotation note names in prose.
 *
 * The bars themselves are not asserted here: Recharts renders inside a
 * `ResponsiveContainer`, which has no dimensions in jsdom — which is exactly how a legend
 * listing two series in the opposite order to the bars shipped through a green suite. What
 * *is* assertable is the collapsed summary and the rotation note, plain DOM either side of
 * it, and the note is still gated on `hasLedger`: with no deposit ledger money in IS
 * deployment, so there is no gap for it to describe.
 */

afterEach(cleanup)

function win(over: Partial<ContributionWindow> = {}): ContributionWindow {
  return {
    label: '12m',
    months: 12,
    partial: false,
    money_in_eur: 24000,
    avg_money_in_per_month_eur: 2000,
    money_in_method: 'spliced',
    deposits_eur: 24000,
    deployed_eur: 30000,
    avg_deployed_per_month_eur: 2500,
    net_eur: 28000,
    ...over,
  }
}

function month(m: string, deployed: number, moneyIn = deployed): ContributionMonthlyItem {
  return { month: m, money_in_eur: moneyIn, deployed_eur: deployed, net_eur: deployed }
}

function data(over: Partial<ContributionsResponse> = {}): ContributionsResponse {
  return {
    windows: [win()],
    monthly: [month('2026-06', 1000), month('2026-07', 9999)],
    first_contribution_date: '2024-05-01',
    deposits_from: '2026-01-09',
    coverage_from: '2026-01-09',
    transfer_in_date: '2026-01-20',
    base_currency: 'EUR',
    ...over,
  }
}

function expand() {
  fireEvent.click(screen.getByRole('button', { name: /Money In per Month/ }))
}

describe('the 12M average comes from the server, not from a second computation', () => {
  it("shows the server's average rather than one derived from the monthly rows", () => {
    // The rows average 5,499.50; the server says 2,000. Only the server's may appear.
    render(<MonthlyDeploymentCard data={data()} isLoading={false} />)
    expect(screen.getByText(/12M avg/)).toBeTruthy()
    expect(screen.getByText(/2,000\/mo/)).toBeTruthy()
    expect(screen.queryByText(/5,50[01]|5,499/)).toBeNull()
  })

  it('is immune to a month with no activity being absent from the rows', () => {
    // A gap makes `slice(-12)` span more calendar months than it divides by. Reading
    // the server's figure means the rows cannot influence the average at all.
    render(
      <MonthlyDeploymentCard
        data={data({ monthly: [month('2026-01', 50000), month('2026-07', 50000)] })}
        isLoading={false}
      />,
    )
    expect(screen.getByText(/2,000\/mo/)).toBeTruthy()
  })

  it('names the window it actually measured when history is short', () => {
    // A four-month-old portfolio has no twelve-month average. The server already
    // clamps the divisor; the label must not still claim twelve.
    render(
      <MonthlyDeploymentCard
        data={data({ windows: [win({ partial: true, months: 4, avg_money_in_per_month_eur: 900 })] })}
        isLoading={false}
      />,
    )
    expect(screen.getByText(/4M avg/)).toBeTruthy()
    expect(screen.queryByText(/12M avg/)).toBeNull()
  })

  it('states a failure rather than reporting no capital deployed', () => {
    render(<MonthlyDeploymentCard data={undefined} isLoading={false} isError />)
    expect(screen.getByText(/Could not load contributions/)).toBeTruthy()
  })
})

describe('the headline figure is money in, not gross deployment', () => {
  it('summarises the latest month as money in and reads the money-in average', () => {
    render(
      <MonthlyDeploymentCard
        data={data({ monthly: [month('2026-08', 31400, 1240)] })}
        isLoading={false}
      />,
    )
    // The month that prompted this: 31,400 deployed, 1,240 actually paid in. The
    // summary is the surface everybody reads, so it must carry the smaller, true one.
    expect(screen.getByText(/1,240 in/)).toBeTruthy()
    expect(screen.queryByText(/31,400/)).toBeNull()
    // And the average is money in too, so it is the same quantity as the strip's.
    expect(screen.getByText(/2,000\/mo/)).toBeTruthy()
    expect(screen.queryByText(/2,500\/mo/)).toBeNull()
  })

  it('still reports the latest month when the server sends no 12m window', () => {
    render(<MonthlyDeploymentCard data={data({ windows: [] })} isLoading={false} />)
    expect(screen.getByText(/9,999 in/)).toBeTruthy()
    expect(screen.queryByText(/avg/)).toBeNull()
  })

  it('falls back to deployment when the backend does not publish money in', () => {
    // Absent means "this backend is older", never "nothing was paid in" — the same
    // reading `unpriced_holdings` makes. A 0 bar would assert the opposite.
    const legacy = data({
      monthly: [{ month: '2026-07', deployed_eur: 9999, net_eur: 9999 }],
    })
    render(<MonthlyDeploymentCard data={legacy} isLoading={false} />)
    expect(screen.getByText(/9,999 deployed/)).toBeTruthy()
    expect(screen.getByText(/2,500\/mo/)).toBeTruthy()   // the deployed average, as before
  })
})

describe('a rotation is named rather than left to be inferred', () => {
  it('names the largest month and what of it was already-invested capital', () => {
    render(
      <MonthlyDeploymentCard
        data={data({
          monthly: [
            month('2026-06', 1000, 1000),
            month('2026-08', 31400, 1240),
            month('2026-09', 3639, 3639),
          ],
        })}
        isLoading={false}
      />,
    )
    expand()
    // The note carries the whole rotation now that no deployed bar is drawn beside it:
    // what went into positions, how much of that was new, and the difference.
    const note = screen.getByText(/rotated between holdings/)
    expect(note.textContent).toMatch(/Aug 26/)
    expect(note.textContent).toMatch(/31,400/)   // deployed
    expect(note.textContent).toMatch(/1,240/)    // of which new money
    expect(note.textContent).toMatch(/30,160/)   // 31,400 - 1,240
  })

  it('names the largest month even when a later one is quiet', () => {
    // The latest month would have been the cheap choice, and the note would then
    // disappear while the spike it explains is still the tallest bar on the chart.
    render(
      <MonthlyDeploymentCard
        data={data({ monthly: [month('2026-08', 31400, 1240), month('2026-09', 500, 500)] })}
        isLoading={false}
      />,
    )
    expand()
    expect(screen.getByText(/rotated between holdings/).textContent).toMatch(/Aug 26/)
  })

  it('says nothing when every month deployed only what came in', () => {
    render(
      <MonthlyDeploymentCard
        data={data({ monthly: [month('2026-06', 1000), month('2026-07', 2000)] })}
        isLoading={false}
      />,
    )
    expand()
    expect(screen.queryByText(/rotated between holdings/)).toBeNull()
  })

  it('says nothing when there is no deposit ledger, where money in IS deployment', () => {
    // Under the 'deployed' method the two series are the same number by construction,
    // so a note about the gap would be describing a gap that cannot exist.
    render(
      <MonthlyDeploymentCard
        data={data({
          windows: [win({ money_in_method: 'deployed' })],
          monthly: [month('2026-08', 31400, 31400)],
        })}
        isLoading={false}
      />,
    )
    expand()
    expect(screen.queryByText(/rotated between holdings/)).toBeNull()
  })
})
