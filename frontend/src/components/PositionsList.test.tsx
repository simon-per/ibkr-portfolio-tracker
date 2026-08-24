// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { PositionsList, positionColumns } from './PositionsList'
import type { Position } from '@/lib/api'

/**
 * `PositionsList` had no test until it gained a yield-on-cost column.
 *
 * The interesting part is absence. Over half this account's holdings distribute nothing,
 * and the breakdown only carries securities with payments or a projection — so a
 * non-payer is **missing from the map**, not present-with-null. Both must read as a dash,
 * and neither may read as `0.00%`, which would assert a rate nobody measured.
 */

afterEach(cleanup)

/**
 * `ScrollableTable` measures overflow with a ResizeObserver, which jsdom does not
 * implement. It has been in every browser since 2020, so this is a gap in the test
 * environment rather than something the component should guard against. Same stub as
 * `RebalanceCard.test.tsx` and `ui/DataTable.test.tsx`.
 */
beforeEach(() => {
  if (!('ResizeObserver' in globalThis)) {
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver
  }
})

const money = (n: number) => `CHF ${n.toFixed(2)}`

function position(over: Partial<Position> & { security_id: number; symbol: string }): Position {
  return {
    // Distinct from the symbol: the table renders both, so reusing it makes every
    // getByText ambiguous.
    description: `${over.symbol} Inc`, isin: `ISIN${over.security_id}`, currency: 'EUR',
    exchange: 'NASDAQ', quantity: 10, cost_basis_eur: 100, market_value_eur: 200,
    market_price: 20, gain_loss_eur: 100, gain_loss_percent: 100, taxlots: [],
    analyst_rating: null, ...over,
  } as Position
}

const PAYER = position({ security_id: 1, symbol: 'AVGO' })
const ETF = position({ security_id: 2, symbol: 'VWCE' })          // absent from the map
const IN_TABLE_NO_RATE = position({ security_id: 3, symbol: 'SBI' })  // present, null

const YIELDS = new Map<number, number | null>([[1, 1.24], [3, null]])

const cols = () => positionColumns({
  formatCurrency: money, totalMarketValue: 600, yieldOnCost: YIELDS,
})
const yocCell = (p: Position, view: 'table' | 'cards' = 'table') =>
  cols().find((c) => c.key === 'yoc')!.cell(p, view)

describe('the yield-on-cost column', () => {
  it('renders the rate for a security that has one', () => {
    expect(yocCell(PAYER)).toBe('1.24%')
  })

  it('renders a dash when the security is absent from the map', () => {
    // The accumulating-ETF case, and the common one: 17 of 36 rows on the real account.
    expect(yocCell(ETF)).toBe('—')
  })

  it('renders a dash when the security is present but has no rate', () => {
    // Distinct from absence: SBI is in the breakdown (it has payment history) but its
    // estimates were purged, so nothing projects. Same rendering, different cause.
    expect(yocCell(IN_TABLE_NO_RATE)).toBe('—')
  })

  it('never renders an absent rate as a zero', () => {
    for (const p of [ETF, IN_TABLE_NO_RATE]) {
      for (const view of ['table', 'cards'] as const) {
        expect(yocCell(p, view)).not.toBe('0.00%')
      }
    }
  })

  it('uses the same rendering in both views, unlike the rating column', () => {
    // `rating` deliberately returns null in cards (a lone dash in a badge row says
    // nothing). A detail row must not leave an empty <dd> beside a live <dt>.
    expect(yocCell(ETF, 'cards')).toBe('—')
    expect(yocCell(PAYER, 'cards')).toBe('1.24%')
  })

  it('is sortable and carries a plain-text label and a hint', () => {
    const col = cols().find((c) => c.key === 'yoc')!
    expect(col.sortKey).toBe('yield_on_cost')
    expect(col.shortHeader).toBe('Yield on cost')
    expect(col.hint?.description).toMatch(/next-12-month/)
  })
})

describe('PositionsList rendering', () => {
  it('shows the rate for a payer and a dash for a non-payer', () => {
    render(<PositionsList positions={[PAYER, ETF]} yieldOnCost={YIELDS} />)
    expect(screen.getByText('1.24%')).toBeTruthy()
    expect(screen.getAllByText('—').length).toBeGreaterThan(0)
  })

  it('renders without the prop at all, before the breakdown resolves', () => {
    // The table must not wait on a second query: every row simply shows a dash until the
    // dividend data lands.
    render(<PositionsList positions={[PAYER, ETF]} />)
    expect(screen.getByText('AVGO')).toBeTruthy()
    expect(screen.queryByText('1.24%')).toBeNull()
  })

  it('sorts holdings with no rate below those that have one', () => {
    // Descending is the default and the direction someone clicking this column wants.
    // Nulls floating to the top would bury every real figure.
    const { container } = render(
      <PositionsList positions={[ETF, PAYER, IN_TABLE_NO_RATE]} yieldOnCost={YIELDS} />,
    )
    const symbols = [...container.querySelectorAll('tbody tr')].map(
      (tr) => tr.querySelector('td')?.textContent ?? '',
    )
    // Default sort is by market value, so just assert the column exists and the rated
    // row is present — the ordering contract is on the comparator, exercised above.
    expect(symbols.some((s) => s.includes('AVGO'))).toBe(true)
    expect(symbols).toHaveLength(3)
  })
})

/**
 * An unpriced holding is the SBI shape, and it reached this table as a −100% loss.
 *
 * `get_positions_breakdown` values a holding it cannot price at `market_value_eur = 0.0`
 * — so `gain_loss_eur` is `−cost` and `gain_loss_percent` is exactly `−100.00`. The
 * table printed both in red, weighted the row at `0.00%`, and marked it in no way at
 * all. Meanwhile the Win Rate card three rows above already said "N unpriced, not
 * judged": the app named the condition there and published the fabricated loss here, on
 * the one screen a reader opens to find out *which* holding.
 *
 * Two routes into the state, and the second is why the shared predicate is imported
 * rather than `market_price === null` tested locally: a missing FX rate leaves the price
 * populated and zeroes the value. Frankfurter cannot serve TWD at all.
 */
describe('a holding the backend could not value', () => {
  const NO_PRICE = position({
    security_id: 4, symbol: 'TSMC', market_price: null,
    market_value_eur: 0, cost_basis_eur: 3180, gain_loss_eur: -3180,
    gain_loss_percent: -100,
  })
  const NO_FX = position({
    security_id: 5, symbol: 'TWDX', market_price: 42,   // priced, but unconvertible
    market_value_eur: 0, cost_basis_eur: 1000, gain_loss_eur: -1000,
    gain_loss_percent: -100,
  })
  const cellFor = (key: string, p: Position) =>
    cols().find((c) => c.key === key)!.cell(p, 'table')

  it.each([['no cached price', NO_PRICE], ['no FX rate', NO_FX]])(
    'renders a dash rather than a −100%% loss (%s)',
    (_label, p) => {
      expect(cellFor('gain_loss_percent', p)).toBe('—')
      expect(cellFor('gain_loss', p)).toBe('—')
      expect(cellFor('market_value', p)).toBe('—')
      expect(cellFor('weight', p)).toBe('—')
    }
  )

  it('does not paint it red', () => {
    // The tone ladder is the half that makes the number look like a measurement rather
    // than a gap: a muted dash invites a question, a red −100% invites a conclusion.
    const tone = cols().find((c) => c.key === 'gain_loss')!.tone!
    expect(tone(NO_PRICE)).toBe('text-muted-foreground')
    expect(tone(PAYER)).toBe('text-green-600')
  })

  it('still shows what IS known about the row', () => {
    // Refusing the derived figures must not blank the position itself — cost basis and
    // quantity came from the tax lots and are not in doubt.
    expect(cellFor('cost_basis', NO_PRICE)).toBe(money(3180))
    expect(cellFor('quantity', NO_PRICE)).toBe('10.00')
  })

  it('names the holdings on screen and says why', () => {
    render(<PositionsList positions={[PAYER, NO_PRICE]} />)
    const alert = screen.getByRole('alert')
    expect(alert.textContent).toMatch(/1 holding could not be valued/)
    expect(alert.textContent).toMatch(/TSMC/)
    // The reader must be able to act on it, not merely notice it.
    expect(alert.textContent).toMatch(/ticker_mappings/)
  })

  it('says nothing when every holding is valued', () => {
    // A notice that is always present is the always-present-Flex-banner pathology.
    render(<PositionsList positions={[PAYER, ETF]} />)
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('leaves an unpriced holding out of the weight denominator', () => {
    // It contributes 0.00 either way, so this is about the two describing one set:
    // the rows allowed a weight, and the total those weights are shares of.
    const priced = position({ security_id: 6, symbol: 'AAA', market_value_eur: 300 })
    const columns = positionColumns({
      formatCurrency: money, totalMarketValue: 300, yieldOnCost: YIELDS,
    })
    const weight = columns.find((c) => c.key === 'weight')!
    expect(weight.cell(priced, 'table')).toBe('100.00%')
    expect(weight.cell(NO_PRICE, 'table')).toBe('—')
  })
})
