// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import type { Column } from './DataTable'
import { positionColumns } from '../PositionsList'
import { watchlistColumns } from '../watchlistColumns'
import { lookthroughColumns } from '../LookThroughTab'

/**
 * The family test CLAUDE.md asks for: written against "every table that renders through
 * DataTable", not against any one of them.
 *
 * `DataTable.test.tsx` pins the primitive's behaviour with a synthetic fixture. This
 * pins the *conventions* the real column sets have to follow — the ones that are not
 * expressible in the type system and that a twelfth table would otherwise reinvent or,
 * more likely, skip.
 *
 * It cannot see a table that never registers here. That is a real limit, stated rather
 * than papered over; `noRawTables` below is the backstop that catches a component going
 * around the primitive entirely.
 */

/**
 * Stubs that match the real formatters' contracts, including their null handling —
 * a stub that throws on null tests the stub, not the column.
 */
const money = (n: number) => `€${n.toFixed(2)}`
const moneyOrDash = (value: number | null) => (value === null ? '-' : `€${value.toFixed(2)}`)

/**
 * Only the column sets exported as factories can be checked. The four tax tables and
 * the activity/fundamentals/dividends sets are module-local by design — they close over
 * nothing and are exercised through their components — so `noRawTables` is what covers
 * them.
 */
/**
 * Each entry carries a row with every *nullable* field actually null — the shape the
 * API really produces for an unsynced security, and the one that used to take a whole
 * tab down when a cell assumed a value. Fields the type declares non-nullable are given
 * a value, because a missing key is a state the API cannot reach and asserting on it
 * would be a phantom failure.
 */
const SPARSE_POSITION = {
  security_id: 1,
  symbol: 'X',
  description: '',
  isin: '',
  exchange: null,
  quantity: 0,
  cost_basis_eur: 0,
  market_value_eur: 0,
  gain_loss_eur: 0,
  gain_loss_percent: 0,
  market_price: null,
  analyst_rating: null,
}

const SPARSE_WATCHLIST = {
  id: 1,
  yahoo_ticker: 'X',
  symbol: null,
  company_name: null,
  notes: null,
  target_price: null,
  current_price: null,
  currency: null,
  trailing_pe: null,
  forward_pe: null,
  peg_ratio: null,
  ev_to_ebitda: null,
  revenue_growth: null,
  earnings_growth: null,
  fwd_revenue_growth: null,
  fwd_eps_growth: null,
  profit_margins: null,
  market_cap: null,
  analyst_target: null,
  analyst_rating: null,
  analyst_count: null,
  week52_high: null,
  week52_low: null,
  pct_from_52w_high: null,
  ma200: null,
  ma50: null,
  pct_from_ma200: null,
  rsi14: null,
  buy_score: null,
  data_currency: null,
}

const SPARSE_LOOKTHROUGH = {
  company_key: 'XX0000000000',
  key_type: 'unidentified',
  name: 'Unknown Holding',
  value_eur: 0,
  pct_of_portfolio: 0,
  direct_value_eur: 0,
  via_funds_value_eur: 0,
  isins: [],
  listings: [],
  via_funds: [],
  partially_resolved: false,
  identity_conflicts: [],
}

const FAMILY: Array<[string, Column<never, string>[], unknown]> = [
  [
    'Positions',
    // An EMPTY yield map on purpose: a security with no payments and no projection is
    // absent from the breakdown rather than present-with-null, and that absence is the
    // sparse case the null-safety assertion below has to cover. Passing a populated map
    // would exercise the easy path only.
    positionColumns({
      formatCurrency: money, totalMarketValue: 1000, yieldOnCost: new Map(),
    }) as never,
    SPARSE_POSITION,
  ],
  ['Watchlist', watchlistColumns({ formatCurrency: moneyOrDash }) as never, SPARSE_WATCHLIST],
  [
    'Look-through',
    lookthroughColumns({ formatCurrency: money }) as never,
    // The sparse case that actually occurs: a company reached ONLY through funds has no
    // directly-held listing, and one reached only directly has no contributing funds. Both
    // arrays empty at once is the shape a cell must not index into blindly.
    SPARSE_LOOKTHROUGH,
  ],
]

describe.each(FAMILY)('%s columns follow the shared conventions', (_name, columns, sparseRow) => {
  it('has exactly one title column — the card needs a heading and only one', () => {
    expect(columns.filter(c => c.mobile === 'title')).toHaveLength(1)
  })

  it('has at most one value and at most one delta', () => {
    expect(columns.filter(c => c.mobile === 'value').length).toBeLessThanOrEqual(1)
    expect(columns.filter(c => c.mobile === 'delta').length).toBeLessThanOrEqual(1)
  })

  it('has unique keys, which the footer spans and React both rely on', () => {
    const keys = columns.map(c => c.key)
    expect(new Set(keys).size).toBe(keys.length)
  })

  it('gives every sortable column a plain-text label', () => {
    // The phone's sort control is a native <select>, and an <option> cannot hold JSX.
    for (const col of columns.filter(c => c.sortKey)) {
      const label = col.shortHeader ?? (typeof col.header === 'string' ? col.header : null)
      expect(label, `${col.key} is sortable but has no string label`).toBeTruthy()
    }
  })

  it('never hides a column from both renderings', () => {
    // A column visible nowhere is a mistake, not a configuration.
    for (const col of columns) {
      expect(
        col.mobile === 'hide' && col.desktop === 'hide',
        `${col.key} is hidden in both modes`,
      ).toBe(false)
    }
  })

  it('renders every cell without throwing on an all-null row', () => {
    // An unsynced watchlist entry really is almost entirely null, and a cell that
    // assumes a value takes the whole tab down rather than showing one blank column.
    const sparse = sparseRow as never
    for (const col of columns) {
      expect(() => col.cell(sparse, 'table'), `${col.key} (table)`).not.toThrow()
      expect(() => col.cell(sparse, 'cards'), `${col.key} (cards)`).not.toThrow()
    }
  })
})

describe('no component renders a table outside the primitive', () => {
  it('leaves raw <table> markup only in ui/', async () => {
    // The backstop for column sets this file cannot import. A new `<table>` under
    // components/ means a surface that will be a spreadsheet on a phone again — the
    // exact regression this whole body of work removed.
    const sources = import.meta.glob('../*.tsx', { query: '?raw', import: 'default', eager: true })
    const offenders = Object.entries(sources)
      .filter(([path]) => !path.endsWith('.test.tsx'))
      .filter(([, src]) => /<table[\s>]/.test(src as string))
      .map(([path]) => path)

    // Three deliberate exceptions, each argued in the file that keeps it. Adding to
    // this list should feel like it needs a reason, because it does.
    expect(offenders.sort(), 'render these through ui/DataTable instead').toEqual([
      // A currency breakdown has no identity column to promote and no headline figure
      // to lead with, and the comparison *between* rows is the point.
      '../CurrencyExposureCard.tsx',
      // Both of these are 12-month x N-year matrices. Cards would buy one honest
      // scroll for a much longer page plus a second rendering of the same data.
      '../DividendSummary.tsx',
      '../MonthlyReturnsHeatmap.tsx',
    ])
  })

  it('has no consumer passing DataTable\'s test-only mode prop', () => {
    const sources = import.meta.glob('../*.tsx', { query: '?raw', import: 'default', eager: true })
    const offenders = Object.entries(sources)
      .filter(([path]) => !path.endsWith('.test.tsx'))
      .filter(([, src]) => /\bmode=["{](?:table|cards)/.test(src as string))
      .map(([path]) => path)
    expect(offenders).toEqual([])
  })
})
