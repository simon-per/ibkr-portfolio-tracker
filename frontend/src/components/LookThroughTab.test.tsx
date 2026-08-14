// @vitest-environment jsdom
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { render, screen, cleanup, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { LookThroughTab } from './LookThroughTab'
import { CurrencyProvider } from '@/lib/CurrencyContext'
import { api } from '@/lib/api'
import type { LookthroughResponse } from '@/lib/api'

/**
 * The arithmetic is pinned server-side in `tests/test_lookthrough_partition.py`. These cover
 * what only the component can get wrong, and the first is the one that matters: the coverage
 * caveat has to be *rendered*, outside any collapsible. It rides on a successful response, so
 * nothing else would ever reveal its absence — and a company table that silently omits a
 * fifth of the book is the exact failure this feature was built to avoid creating.
 */

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

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

function withProviders(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <CurrencyProvider>{node}</CurrencyProvider>
    </QueryClientProvider>,
  )
}

function response(over: Partial<LookthroughResponse> = {}): LookthroughResponse {
  return {
    as_of: '2026-08-14',
    base_currency: 'EUR',
    total_market_value_eur: 5200,
    direct_equity_eur: 1700,
    looked_through_equity_eur: 1800,
    fund_residual_eur: 200,
    nested_fund_eur: 0,
    uncovered_fund_eur: 1500,
    coverage_pct: 67.31,
    companies: [
      {
        company_key: '5493006MHB84DD0ZWV18',
        key_type: 'lei',
        name: 'ALPHABET INC.',
        value_eur: 2300,
        pct_of_portfolio: 44.23,
        direct_value_eur: 1700,
        via_funds_value_eur: 600,
        isins: ['US02079K1079', 'US02079K3059'],
        listings: ['ABEA@IBIS', 'GOOG@NASDAQ', 'GOOGL@NASDAQ'],
        via_funds: [
          { symbol: 'XNAS', fund_isin: 'IE00BMFKG444', value_eur: 600, weight_pct: 30 },
        ],
        partially_resolved: false,
        identity_conflicts: [],
      },
      {
        company_key: 'US67066G1040',
        key_type: 'isin',
        name: 'NVIDIA CORP',
        value_eur: 1200,
        pct_of_portfolio: 23.08,
        direct_value_eur: 0,
        via_funds_value_eur: 1200,
        isins: ['US67066G1040'],
        listings: [],
        via_funds: [
          { symbol: 'XNAS', fund_isin: 'IE00BMFKG444', value_eur: 1200, weight_pct: 60 },
        ],
        partially_resolved: true,
        identity_conflicts: [],
      },
    ],
    companies_shown: 2,
    company_count_total: 40,
    shown_value_eur: 3500,
    other_companies_eur: 0,
    other_companies_count: 38,
    funds: [
      {
        symbol: 'VWCE', fund_isin: 'IE00BK5BQT80', market_value_eur: 1000,
        status: 'no_basket',
        reason: 'No machine-readable holdings file is published for this fund.',
        basket_as_of: null, stale: false, constituents: 0, equity_weight_pct: null,
        residual_eur: 0, asset_class_available: true, source: null,
      },
      {
        symbol: 'XNAS', fund_isin: 'IE00BMFKG444', market_value_eur: 2000,
        status: 'looked_through', reason: null, basket_as_of: '2026-08-13', stale: false,
        constituents: 106, equity_weight_pct: 90, residual_eur: 200,
        asset_class_available: true, source: 'dws',
      },
    ],
    oldest_basket_as_of: '2026-08-13',
    unvaluable_positions: 0,
    unvaluable_symbols: [],
    identity: {
      resolved_by_lei: 1,
      resolved_by_share_class_figi: 0,
      resolved_by_isin: 1,
      unidentified_groups: 0,
      partially_resolved_groups: 1,
      unresolved_isins: 1,
      unresolved_value_eur: 1200,
    },
    warnings: [
      '28.8% of the portfolio sits in funds whose constituents are not known (VWCE, DBPG), so every company figure below excludes whatever those funds hold.',
    ],
    ...over,
  }
}

describe('LookThroughTab', () => {
  it('renders the coverage caveat as an alert, not buried in a collapsible', async () => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(response())
    withProviders(<LookThroughTab />)

    const alert = await screen.findByRole('alert')
    expect(alert).toBeTruthy()
    expect(alert.textContent).toContain('partial view')
    expect(alert.textContent).toContain('VWCE')
    expect(alert.textContent).toContain('28.8%')
  })

  it('folds a company across its listings and shows the direct/via-fund split', async () => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(response())
    withProviders(<LookThroughTab />)

    await screen.findByText('ALPHABET INC.')
    // One row, naming all three listings that were folded into it.
    expect(screen.getByText(/ABEA@IBIS, GOOG@NASDAQ, GOOGL@NASDAQ/)).toBeTruthy()
    expect(screen.getAllByText('44.23%').length).toBeGreaterThan(0)
    // A fund-only company has no direct value and says so with a dash, not a zero.
    expect(screen.getByText('NVIDIA CORP')).toBeTruthy()
  })

  it('names the truncated tail rather than leaving the percentages short', async () => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(response())
    withProviders(<LookThroughTab />)

    await screen.findByText('ALPHABET INC.')
    expect(screen.getByText(/A further 38 companies hold/)).toBeTruthy()
  })

  it('explains the dagger inline when a row was folded on an ISIN alone', async () => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(response())
    withProviders(<LookThroughTab />)

    await screen.findByText('ALPHABET INC.')
    expect(screen.getByText(/folded on an ISIN alone/)).toBeTruthy()
  })

  it('states the value attributed to no company at all', async () => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(response())
    withProviders(<LookThroughTab />)

    await screen.findByText('Fund coverage')
    expect(screen.getByText(/Not attributed to any company/)).toBeTruthy()
    // The fund with no basket is named with its reason rather than omitted.
    expect(screen.getByText('No basket')).toBeTruthy()
  })

  it('reports a failed fetch as a failure instead of an empty portfolio', async () => {
    vi.spyOn(api, 'getLookthrough').mockRejectedValue(new Error('boom'))
    withProviders(<LookThroughTab />)

    await waitFor(() =>
      expect(screen.getByText(/didn't respond/)).toBeTruthy(),
    )
    // Crucially NOT the empty-state copy, which would read as "you own no companies".
    expect(screen.queryByText(/No company exposure yet/)).toBeNull()
  })

  it('treats an empty portfolio as a state, not as an outage', async () => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(
      response({
        total_market_value_eur: 0, direct_equity_eur: 0, looked_through_equity_eur: 0,
        fund_residual_eur: 0, nested_fund_eur: 0, uncovered_fund_eur: 0, coverage_pct: 0,
        companies: [], companies_shown: 0, company_count_total: 0, shown_value_eur: 0,
        other_companies_eur: 0, other_companies_count: 0, funds: [],
        oldest_basket_as_of: null, warnings: [],
      }),
    )
    withProviders(<LookThroughTab />)

    await screen.findByText(/No company exposure yet/)
    expect(screen.queryByText(/didn't respond/)).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('shows a dash rather than a confident 0.0% coverage on an empty book', async () => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(
      response({
        total_market_value_eur: 0, coverage_pct: 0, companies: [], companies_shown: 0,
        company_count_total: 0, funds: [], warnings: [],
      }),
    )
    withProviders(<LookThroughTab />)

    await screen.findByText('Coverage')
    // `0.0%` would read as "nothing could be attributed" on a book that is simply empty.
    expect(screen.queryByText('0.0%')).toBeNull()
    expect(screen.getAllByText('—').length).toBeGreaterThan(0)
  })

  it('offers the row-count choices and asks the backend for the selected one', async () => {
    const spy = vi.spyOn(api, 'getLookthrough').mockResolvedValue(response())
    withProviders(<LookThroughTab />)

    await screen.findByText('ALPHABET INC.')
    expect(spy).toHaveBeenCalledWith(50)
    expect(screen.getByRole('button', { name: 'Top 100' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Top 25' })).toBeTruthy()
  })
})
