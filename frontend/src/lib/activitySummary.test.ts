import { describe, expect, it } from 'vitest'
import { isSell, summarizeActivity } from './activitySummary'
import type { ActivityResponse, ActivityRow } from './api'

function row(over: Partial<ActivityRow>): ActivityRow {
  return {
    date: '2026-09-10', kind: 'trade', subtype: 'BUY', symbol: 'AAA', security_id: 1,
    description: '', quantity: 1, price: 10, currency: 'USD', amount_base: -10,
    realized_pnl_base: null, counts_as_money_in: null, source: null, ib_key: null,
    ...over,
  }
}

function response(items: ActivityRow[], over: Partial<ActivityResponse> = {}): ActivityResponse {
  return {
    items, total: items.length, limit: 100, offset: 0, base_currency: 'EUR',
    start_date: '2025-09-12', end_date: '2026-09-12', ...over,
  }
}

const LEDGER = [
  row({ subtype: 'BUY', amount_base: -100 }),
  row({ subtype: 'BUY', amount_base: -50 }),
  row({ subtype: 'SELL', amount_base: 80, realized_pnl_base: 12 }),
  row({ kind: 'dividend', subtype: 'ibkr', source: 'ibkr', amount_base: 3 }),
  row({ kind: 'cash_flow', subtype: 'DEPOSITWITHDRAW', counts_as_money_in: true, amount_base: 1000 }),
  row({ kind: 'cash_flow', subtype: 'DEPOSITWITHDRAW', counts_as_money_in: true, amount_base: -200 }),
  // A transfer is never money in: whitelisted out by the server's flag.
  row({ kind: 'cash_flow', subtype: 'TRANSFER_IN', counts_as_money_in: false, amount_base: 5000 }),
  row({ kind: 'corporate_action', subtype: 'FORWARDSPLIT', amount_base: null, price: null }),
]

describe('summarizeActivity', () => {
  it('sums a complete window by kind, signed like the Amount column', () => {
    const s = summarizeActivity(response(LEDGER), [])
    expect(s).toEqual({
      events: 8, bought: -150, sold: 80, realized: 12, dividends: 3,
      dividendsEstimated: false, netDeposits: 800,
    })
  })

  it('never sums a page of a longer window: every money figure is unknown', () => {
    const s = summarizeActivity(response(LEDGER.slice(0, 4), { total: 270 }), [])
    expect(s.events).toBe(270)
    expect([s.bought, s.sold, s.realized, s.dividends, s.netDeposits]).toEqual([null, null, null, null, null])
  })

  it('treats a later page as incomplete even when its rows happen to match the total', () => {
    const s = summarizeActivity(response(LEDGER, { offset: 100 }), [])
    expect(s.bought).toBeNull()
  })

  it('leaves a filtered-out kind unknown rather than zero', () => {
    const tradesOnly = LEDGER.filter(r => r.kind === 'trade')
    const s = summarizeActivity(response(tradesOnly), ['trade'])
    expect(s.bought).toBe(-150)
    expect(s.dividends).toBeNull()
    expect(s.netDeposits).toBeNull()
  })

  it('is unknown when any row it would add has no amount', () => {
    const s = summarizeActivity(response([...LEDGER, row({ subtype: 'BUY', amount_base: null })]), [])
    expect(s.bought).toBeNull()
    expect(s.sold).toBe(80)
  })

  it('is unknown when a sale has no realized figure', () => {
    const s = summarizeActivity(response([...LEDGER, row({ subtype: 'SELL', amount_base: 5, realized_pnl_base: null })]), [])
    expect(s.realized).toBeNull()
    expect(s.sold).toBe(85)
  })

  it('flags estimated dividends in the sum', () => {
    const est = row({ kind: 'dividend', subtype: 'yfinance_estimate', source: 'yfinance_estimate', amount_base: 2 })
    const s = summarizeActivity(response([...LEDGER, est]), [])
    expect(s.dividends).toBe(5)
    expect(s.dividendsEstimated).toBe(true)
  })

  it('sums an empty kind in a complete window to zero', () => {
    const s = summarizeActivity(response([]), [])
    expect(s).toMatchObject({ events: 0, bought: 0, dividends: 0, netDeposits: 0 })
  })
})

describe('isSell', () => {
  it('reads IBKR subtypes, and never calls a non-trade a sale', () => {
    expect(isSell(row({ subtype: 'SELL' }))).toBe(true)
    expect(isSell(row({ subtype: 'sell (Ca.)' }))).toBe(true)
    expect(isSell(row({ subtype: 'BUY' }))).toBe(false)
    expect(isSell(row({ kind: 'cash_flow', subtype: 'SELL' }))).toBe(false)
  })
})
