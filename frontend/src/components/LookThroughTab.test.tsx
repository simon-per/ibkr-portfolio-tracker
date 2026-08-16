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

describe('LookThroughTab coverage tone', () => {
  /**
   * The threshold this replaces was `coverage_pct >= 95`, which was unreachable: DBPG is ~3.8% of
   * the book and excluded by design, and no basket attributes 100% of its fund, so the practical
   * ceiling is ~95.1-95.9%. The card would have sat amber forever — the always-present-banner
   * pathology. These pin the reachable rule instead, from both sides.
   */
  function coverageCard() {
    const label = screen.getByText('Coverage')
    // KpiCard renders label and value as siblings inside the card body.
    return label.closest('div')?.parentElement as HTMLElement
  }

  it('is not green while any fund has no basket, however high the percentage', async () => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(
      response({
        // Deliberately ABOVE the old 95% threshold, to prove the percentage is not what decides it.
        coverage_pct: 97.4,
        funds: [
          {
            symbol: 'VWCE', fund_isin: 'IE00BK5BQT80', market_value_eur: 1000,
            status: 'no_basket', reason: 'No machine-readable holdings file.',
            basket_as_of: null, stale: false, constituents: 0, equity_weight_pct: null,
            residual_eur: 0, asset_class_available: true, source: null,
          },
        ],
      }),
    )
    withProviders(<LookThroughTab />)

    await screen.findByText('Coverage')
    expect(coverageCard().querySelector('.text-green-600')).toBeNull()
    expect(coverageCard().querySelector('.text-yellow-600')).toBeTruthy()
    expect(screen.getByText(/1 fund\(s\) have no basket/)).toBeTruthy()
  })

  it('is green when the only undecomposed fund is the deliberately excluded one', async () => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(
      response({
        // Deliberately BELOW the old threshold — this is the state it could never reach.
        coverage_pct: 92.1,
        funds: [
          {
            symbol: 'XNAS', fund_isin: 'IE00BMFKG444', market_value_eur: 2000,
            status: 'looked_through', reason: null, basket_as_of: '2026-08-13', stale: false,
            constituents: 106, equity_weight_pct: 100, residual_eur: 0,
            asset_class_available: true, source: 'dws',
          },
          {
            symbol: 'DBPG', fund_isin: 'LU0411078552', market_value_eur: 2703,
            status: 'excluded',
            reason: 'Synthetic swap-based fund: the basket it publishes is collateral.',
            basket_as_of: null, stale: false, constituents: 0, equity_weight_pct: null,
            residual_eur: 0, asset_class_available: true, source: null,
          },
        ],
      }),
    )
    withProviders(<LookThroughTab />)

    await screen.findByText('Coverage')
    expect(coverageCard().querySelector('.text-green-600')).toBeTruthy()
    expect(screen.getByText(/every fund decomposed or deliberately excluded/)).toBeTruthy()
  })

  it('stays green but names ageing baskets, since coverage_pct cannot show that', async () => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(
      response({
        coverage_pct: 92.1,
        funds: [
          {
            symbol: 'VWCE', fund_isin: 'IE00BK5BQT80', market_value_eur: 6510,
            status: 'looked_through', reason: null, basket_as_of: '2026-03-31', stale: true,
            constituents: 3500, equity_weight_pct: 99, residual_eur: 60,
            asset_class_available: false, source: 'manual',
          },
        ],
      }),
    )
    withProviders(<LookThroughTab />)

    await screen.findByText('Coverage')
    // Every fund IS decomposed, so the card is not amber - but a hand-imported basket rots
    // invisibly, and the percentage never moves when it does.
    expect(coverageCard().querySelector('.text-green-600')).toBeTruthy()
    expect(screen.getByText(/1 basket\(s\) ageing/)).toBeTruthy()
  })

  /*
   * The charts. jsdom gives `ResponsiveContainer` a zero-width parent, so the treemap's own
   * SVG never renders here — which is fine, because the tile arithmetic is pinned in
   * `lib/lookthroughChart.test.ts` and what a component test can uniquely see is whether the
   * *wording* around the chart survives. That wording is the honest half: a legend that names
   * each colour, and a bar whose accessible name states the share no company row accounts for.
   */
  it('leads with a composition bar naming the share no company row accounts for', async () => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(response())
    withProviders(<LookThroughTab />)

    await screen.findByText('Where the value sits')
    const bar = screen.getByRole('img', { name: /Portfolio composition/ })
    // 1700 direct, 1800 through funds, and 1500 + 200 + 0 attributed to nothing, of 5200.
    expect(bar.getAttribute('aria-label')).toContain('Held directly 32.7%')
    expect(bar.getAttribute('aria-label')).toContain('Through funds 34.6%')
    expect(bar.getAttribute('aria-label')).toContain('Not attributed 32.7%')
  })

  it('names every colour it uses, so nothing on the charts is carried by hue alone', async () => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(response())
    withProviders(<LookThroughTab />)

    await screen.findByText('Company exposure')
    // Alphabet is held both ways and Nvidia only through a fund — the distinction the whole
    // feature exists to make — plus the grey that keeps the areas shares of the whole book.
    expect(screen.getAllByText('Direct + funds').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Funds only').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Not attributed').length).toBeGreaterThan(0)
    // Nothing here is held direct-only, and a legend naming a colour with no tile invites the
    // reader to hunt for one.
    expect(screen.queryByText('Direct only')).toBeNull()
  })

  it('does not reuse one phrase for two meanings across the bar and the treemap', async () => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(response())
    withProviders(<LookThroughTab />)

    await screen.findByText('Company exposure')
    // The bar splits value and the treemap classifies companies, so "Held directly" (a share
    // of the book) and "Direct only" (a company with no fund component) must stay separate
    // words. Alphabet is in both charts at once and belongs to different groups in each.
    expect(screen.getAllByText('Held directly')).toHaveLength(1)
    expect(screen.queryByText('Both')).toBeNull()
  })

  it('draws no chart at all when nothing could be priced', async () => {
    // A zero-total book has an unknown shape. Rectangles drawn from it would be confident and
    // wrong, which is the `concentrationPct` refusal rather than a missing empty state.
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(
      response({
        total_market_value_eur: 0,
        direct_equity_eur: 0,
        looked_through_equity_eur: 0,
        fund_residual_eur: 0,
        nested_fund_eur: 0,
        uncovered_fund_eur: 0,
        coverage_pct: 0,
        companies: [],
        companies_shown: 0,
        shown_value_eur: 0,
      }),
    )
    withProviders(<LookThroughTab />)

    await screen.findByText('Company exposure')
    expect(screen.queryByText('Where the value sits')).toBeNull()
    expect(screen.queryByRole('img', { name: /Portfolio composition/ })).toBeNull()
  })
})
