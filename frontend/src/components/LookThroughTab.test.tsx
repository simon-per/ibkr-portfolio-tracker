// @vitest-environment jsdom
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { act, render, screen, cleanup, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { LookThroughTab } from './LookThroughTab'
import { unresolvedTone } from '@/lib/lookthroughTone'
import { CurrencyProvider } from '@/lib/CurrencyProvider'
import { useBaseCurrency } from '@/lib/CurrencyContext'
import { api } from '@/lib/api'
import type { LookthroughResponse } from '@/lib/api'

/**
 * The arithmetic is pinned server-side in `tests/test_lookthrough_partition.py`. These cover
 * what only the component can get wrong, and the first is the one that matters: the coverage
 * *qualifier* has to be rendered outside any collapsible — on the Coverage KPI card — while
 * the itemised notes live inside the collapsed Fund coverage card at the bottom and must be
 * reachable from it. The qualifier rides on a successful response, so nothing else would ever
 * reveal its absence — and a company table that silently omits a fifth of the book is the
 * exact failure this feature was built to avoid creating.
 */

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  // Sections remember being opened (`useRememberedOpen`); each test starts closed.
  localStorage.clear()
})

// `ScrollableTable` measures overflow with a ResizeObserver, which jsdom does not implement.
beforeEach(() => {
  // Every company avatar reads the logo manifest; none stored unless a test says otherwise.
  vi.spyOn(api, 'getLogos').mockResolvedValue({ logos: {}, isins: {} })
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
    cash_eur: null,
    cash_source: 'unknown',
    total_value_eur: 5200,
    cash_pct_of_portfolio: null,
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
        sector: 'Communications',
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
        sector: 'Technology',
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
        proxy_for_symbol: null, unweighted_constituents: 0,
      },
      {
        symbol: 'XNAS', fund_isin: 'IE00BMFKG444', market_value_eur: 2000,
        status: 'looked_through', reason: null, basket_as_of: '2026-08-13', stale: false,
        constituents: 106, equity_weight_pct: 90, residual_eur: 200,
        asset_class_available: true, source: 'dws',
        proxy_for_symbol: null, unweighted_constituents: 0,
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
      '28.9% of the invested value sits in funds whose constituents are not known (VWCE, DBPG), so every company figure below excludes whatever those funds hold.',
    ],
    ...over,
  }
}

/** The collapsed Fund coverage card, opened. Its header is a real button, so a click reaches it. */
async function openFundCoverage() {
  fireEvent.click(await screen.findByRole('button', { name: /Fund coverage/ }))
}

/**
 * The KPI tile a label sits in: the child of the panel's grid. Climbing a fixed number of
 * parents broke the day the tile gained an icon well around its label.
 */
function kpiTile(label: HTMLElement): HTMLElement {
  let el: HTMLElement = label
  while (el.parentElement && !el.parentElement.classList.contains('grid')) el = el.parentElement
  return el
}

describe('LookThroughTab insight strip', () => {
  it('shows concentration as shares of the whole book, never of the covered part', async () => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(response())
    withProviders(<LookThroughTab />)

    // Two companies at 44.23% and 23.08% of a book that is only 67% covered: the top-N share
    // is their sum, 67.3%, not 100%.
    expect(within(kpiTile(await screen.findByText('Largest exposure'))).getByText('44.23%')).toBeTruthy()
    expect(within(kpiTile(screen.getByText('Top 2'))).getByText('67.3%')).toBeTruthy()
    const viaFunds = kpiTile(screen.getByText('Largest via funds only'))
    expect(within(viaFunds).getByText('23.08%')).toBeTruthy()
    expect(within(viaFunds).getByText('NVIDIA')).toBeTruthy()
    const both = kpiTile(screen.getByText('Held both ways'))
    expect(within(both).getByText('1')).toBeTruthy()
    expect(within(both).getByText(/via funds/)).toBeTruthy()
  })

  it('shows dashes, not zeros, for a book with nothing to rank', async () => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(response({ total_market_value_eur: 0, total_value_eur: 0 }))
    withProviders(<LookThroughTab />)

    const largest = kpiTile(await screen.findByText('Largest exposure'))
    expect(largest.textContent).toContain('—')
    expect(largest.textContent).not.toContain('%')
  })
})

describe('LookThroughTab constituent logos', () => {
  it('shows a company logo by whichever of its ISINs has one stored', async () => {
    vi.spyOn(api, 'getLogos').mockResolvedValue({ logos: {}, isins: { US02079K3059: 'v1' } })
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(response())
    const { container } = withProviders(<LookThroughTab />)

    await waitFor(() => {
      const srcs = [...container.querySelectorAll('img')].map((img) => img.getAttribute('src') ?? '')
      expect(srcs.some((src) => src.includes('/api/logos/isin/US02079K3059?v=v1'))).toBe(true)
    })
  })
})

describe('LookThroughTab', () => {
  it('keeps the qualifier on the Coverage card and the itemised notes in the collapsed card', async () => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(response())
    withProviders(<LookThroughTab />)

    // Above the fold: the KPI footnote says a fund has no basket, without the ten-line list.
    await screen.findByText('Coverage')
    expect(screen.getByText(/1 fund without holdings/)).toBeTruthy()
    expect(screen.queryByText(/28\.8%/)).toBeNull()

    // The backend's warning paragraphs stay off the screen (owner, 2026-10-14), opened or not.
    const header = screen.getByRole('button', { name: /Fund coverage/ })
    expect(header.getAttribute('aria-expanded')).toBe('false')
    await openFundCoverage()
    expect(header.getAttribute('aria-expanded')).toBe('true')
    expect(screen.queryByRole('list', { name: 'Warnings' })).toBeNull()
  })

  it('keeps the fund table below the company table, last on the page', async () => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(response())
    withProviders(<LookThroughTab />)

    await screen.findAllByText('Alphabet')
    const companies = screen.getByText('Company exposure')
    const coverage = screen.getByRole('button', { name: /Fund coverage/ })
    // DOCUMENT_POSITION_FOLLOWING: the coverage header comes after the company card's title.
    expect(companies.compareDocumentPosition(coverage) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('folds a company across its listings and shows the direct/via-fund split', async () => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(response())
    withProviders(<LookThroughTab />)

    await screen.findAllByText('Alphabet')
    // One row, naming all three listings that were folded into it.
    expect(screen.getByText('ABEA, GOOG, GOOGL')).toBeTruthy()
    expect(screen.getAllByText('44.23%').length).toBeGreaterThan(0)
    // A fund-only company has no direct value and says so with a dash, not a zero.
    expect(screen.getAllByText('NVIDIA').length).toBeGreaterThan(0)
  })

  /**
   * Reported by the owner: pressing Breakdown showed the positions "below the whole block
   * of holdings" rather than under the row. It did — the panel was rendered outside the
   * card that holds the table, so at the default Top 50 the answer landed after row 50 and
   * both footnotes, and the button looked inert.
   *
   * **These assert placement, not presence.** The obvious check — is "Held directly as" on
   * the page — passed against the bug for the whole time it existed, because the panel was
   * always in the document, just in the wrong place.
   */
  it('opens the breakdown inside the row that asked for it', async () => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(response())
    withProviders(<LookThroughTab />)

    await screen.findAllByText('Alphabet')
    const rows = () => Array.from(document.querySelectorAll('tbody tr'))
    expect(rows().some((r) => r.textContent?.includes('Held directly as'))).toBe(false)

    const alphabetRow = rows().find((r) => r.textContent?.includes('Alphabet'))!
    fireEvent.click(within(alphabetRow as HTMLElement).getByRole('button', { name: 'Breakdown' }))

    // The panel is the row IMMEDIATELY after Alphabet's, not somewhere further down.
    const after = rows()
    const owner = after.findIndex((r) => r.textContent?.includes('Alphabet'))
    expect(after[owner + 1]?.textContent).toContain('Held directly as')
    expect(after[owner + 1]?.textContent).toContain('XNAS')
  })

  it('opens the breakdown for the row pressed, and only that row', async () => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(response())
    withProviders(<LookThroughTab />)

    await screen.findAllByText('NVIDIA')
    const rows = () => Array.from(document.querySelectorAll('tbody tr'))
    const nvidiaRow = rows().find((r) => r.textContent?.includes('NVIDIA'))!
    fireEvent.click(within(nvidiaRow as HTMLElement).getByRole('button', { name: 'Breakdown' }))

    // Nvidia is fund-only, so its panel names the fund and no direct listing.
    const after = rows()
    const owner = after.findIndex((r) => r.textContent?.includes('NVIDIA'))
    expect(after[owner + 1]?.textContent).toContain('Held through funds')
    expect(after[owner + 1]?.textContent).not.toContain('Held directly as')
    // Exactly one panel open — the toggle is not additive.
    expect(after.filter((r) => r.textContent?.includes('Held through funds'))).toHaveLength(1)
  })

  it('closes it again from the same control', async () => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(response())
    withProviders(<LookThroughTab />)

    await screen.findAllByText('Alphabet')
    const rowFor = (name: string) =>
      Array.from(document.querySelectorAll('tbody tr')).find((r) =>
        r.textContent?.includes(name),
      ) as HTMLElement

    fireEvent.click(within(rowFor('Alphabet')).getByRole('button', { name: 'Breakdown' }))
    const hide = within(rowFor('Alphabet')).getByRole('button', { name: 'Hide' })
    expect(hide.getAttribute('aria-expanded')).toBe('true')

    fireEvent.click(hide)
    expect(document.body.textContent).not.toContain('Held directly as')
  })

  it('names the truncated tail rather than leaving the percentages short', async () => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(response())
    withProviders(<LookThroughTab />)

    await screen.findAllByText('Alphabet')
    expect(screen.getByText(/\+38 more/)).toBeTruthy()
  })

  it('does not footnote a row folded on an ISIN alone (owner, 2026-10-14)', async () => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(response())
    withProviders(<LookThroughTab />)

    await screen.findAllByText('Alphabet')
    expect(screen.queryByText(/folded on an ISIN alone/)).toBeNull()
  })

  it('carries the fund table as figures, without the prose beneath it (owner, 2026-10-14)', async () => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(response())
    withProviders(<LookThroughTab />)

    await openFundCoverage()
    // The fund with no basket is still named, by its status.
    expect(screen.getByText('No data')).toBeTruthy()
    // Its reason sentence and the not-attributed paragraph are gone; that figure is the
    // composition bar's third segment.
    expect(screen.queryByText(/No machine-readable holdings file/)).toBeNull()
    expect(screen.queryByText(/Not attributed to any company/)).toBeNull()
  })

  it('draws a stale basket date amber, with no dagger beside it', async () => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(
      response({
        funds: [
          {
            symbol: 'VWCE', fund_isin: 'IE00BK5BQT80', market_value_eur: 6510,
            status: 'looked_through', reason: null, basket_as_of: '2026-03-31', stale: true,
            constituents: 3500, equity_weight_pct: 99, residual_eur: 60,
            asset_class_available: false, source: 'manual',
            proxy_for_symbol: null, unweighted_constituents: 0,
          },
        ],
      }),
    )
    withProviders(<LookThroughTab />)

    await openFundCoverage()
    const date = screen.getByText('2026-03-31')
    expect(date.className).toContain('text-amber-600')
    expect(date.textContent).toBe('2026-03-31')
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
        total_market_value_eur: 0, total_value_eur: 0, direct_equity_eur: 0,
        looked_through_equity_eur: 0,
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

  // The server publishes `null` for an empty book since 2026-10-17 and `0` before it; both
  // must read as absent, never as a confident "nothing could be attributed".
  it.each([
    ['null', null],
    ['0 (an older server)', 0],
  ])('shows a dash rather than a confident 0.0%% coverage on an empty book, coverage %s', async (_label, coverage) => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(
      response({
        total_market_value_eur: 0, total_value_eur: 0, coverage_pct: coverage, companies: [], companies_shown: 0,
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

    await screen.findAllByText('Alphabet')
    expect(spy).toHaveBeenCalledWith(50)
    expect(screen.getByRole('button', { name: 'Top 100' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Top 25' })).toBeTruthy()
  })
})

describe('LookThroughTab unvaluable holdings', () => {
  /**
   * `unvaluable_positions` and `unvaluable_symbols` rode on every response and were read
   * nowhere: a holding with no usable price is in neither the company table nor the
   * coverage denominator, so every percentage on the page was a share of a smaller book
   * than the one held, with nothing on the surface saying so — while the backend's own
   * schema docstring asserted the frontend showed it. Same alert `AllocationTab` renders
   * for the same absence.
   */
  it('states, above the KPI row and outside the collapsed card, which holdings it excludes', async () => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(
      response({ unvaluable_positions: 2, unvaluable_symbols: ['SBI', 'TWSE'] }),
    )
    withProviders(<LookThroughTab />)

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/excludes 2 holdings with no usable price/)
    expect(alert.textContent).toMatch(/SBI, TWSE/)
    expect(alert.textContent).toMatch(/share of what could be valued/)
    // On the surface: the Fund coverage card is still collapsed, and the alert precedes
    // the Coverage KPI it qualifies.
    expect(screen.getByRole('button', { name: /Fund coverage/ }).getAttribute('aria-expanded')).toBe('false')
    const coverage = screen.getByText('Coverage')
    expect(alert.compareDocumentPosition(coverage) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('uses the singular for one holding', async () => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(
      response({ unvaluable_positions: 1, unvaluable_symbols: ['SBI'] }),
    )
    withProviders(<LookThroughTab />)
    expect((await screen.findByRole('alert')).textContent).toMatch(/1 holding with no usable price \(SBI\)/)
  })

  it('renders no alert when every holding could be valued', async () => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(response())
    withProviders(<LookThroughTab />)
    await screen.findByText('Coverage')
    expect(screen.queryByRole('alert')).toBeNull()
  })
})

describe('LookThroughTab uninvested cash (GitHub #28)', () => {
  // Holdings 5,200 plus cash 400: every share on the page is of 5,600, as on Allocation.
  const withCash = (over: Partial<LookthroughResponse> = {}) =>
    response({
      cash_eur: 400,
      cash_source: 'derived',
      total_value_eur: 5600,
      cash_pct_of_portfolio: 7.14,
      ...over,
    })

  it('closes the company table with a Cash row under the same columns', async () => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(withCash())
    withProviders(<LookThroughTab />)

    await screen.findAllByText('Alphabet')
    const footer = document.querySelector('tfoot')
    expect(footer?.textContent).toContain('Cash')
    expect(footer?.textContent).toContain('7.14%')
    // A figure, not an explanation (owner, 2026-10-14).
    expect(footer?.textContent).not.toMatch(/uninvested|denominator/i)
  })

  it('draws Cash in both charts, so their shares are of the same whole as the table', async () => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(withCash())
    withProviders(<LookThroughTab />)

    const bar = await screen.findByRole('img', { name: /Portfolio composition/ })
    expect(bar.getAttribute('aria-label')).toContain('Cash 7.1%')
    // 1,700 of 5,600, not of the 5,200 in holdings.
    expect(bar.getAttribute('aria-label')).toContain('Held directly 30.4%')
  })

  it.each([
    ['unknown', { cash_eur: null, cash_source: 'unknown' as const }],
    ['negative', { cash_eur: -250, cash_source: 'derived' as const }],
  ])('has no Cash row when the balance is %s', async (_label, over) => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(
      withCash({ ...over, total_value_eur: 5200, cash_pct_of_portfolio: null }),
    )
    withProviders(<LookThroughTab />)

    await screen.findAllByText('Alphabet')
    expect(document.querySelector('tfoot')).toBeNull()
  })

  it("raises Allocation's alert for a negative balance, and none for an unknown one", async () => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(
      withCash({ cash_eur: -250, total_value_eur: 5200, cash_pct_of_portfolio: null }),
    )
    withProviders(<LookThroughTab />)
    expect((await screen.findByRole('alert')).textContent).toMatch(/negative cash balance/)
    cleanup()

    vi.spyOn(api, 'getLookthrough').mockResolvedValue(response())
    withProviders(<LookThroughTab />)
    await screen.findByText('Coverage')
    expect(screen.queryByRole('alert')).toBeNull()
  })
})

describe('LookThroughTab coverage tone', () => {
  /**
   * The threshold this replaces was `coverage_pct >= 95`, which was unreachable: DBPG is ~3.8% of
   * the book and excluded by design, and no basket attributes 100% of its fund, so the practical
   * ceiling is ~95.1-95.9%. The card would have sat amber forever — the always-present-banner
   * pathology. These pin the reachable rule instead, from both sides.
   */
  it('shows no Unidentified card below 1% of the book — the normal handful of ISINs', async () => {
    // 15 of 5,200 is about 0.3%, the share this account actually carries: amber at any value
    // above zero was amber always.
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(
      response({ identity: { ...response().identity, unresolved_value_eur: 15 } }),
    )
    withProviders(<LookThroughTab />)

    await screen.findByText('Coverage')
    expect(screen.queryByText('Unidentified')).toBeNull()
  })

  it('shows an amber Unidentified card at 1% of the book or more', async () => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(
      response({ identity: { ...response().identity, unresolved_value_eur: 52 } }),
    )
    withProviders(<LookThroughTab />)

    const label = await screen.findByText('Unidentified')
    expect(kpiTile(label).querySelector('.text-yellow-600')).toBeTruthy()
  })

  it('decides the Unresolved tone on the share of the book', () => {
    expect(unresolvedTone(0, 5200)).toBe('neutral')
    expect(unresolvedTone(51.99, 5200)).toBe('neutral')
    expect(unresolvedTone(52, 5200)).toBe('warning')
    expect(unresolvedTone(10, 0)).toBe('neutral')
  })

  function coverageCard() {
    return kpiTile(screen.getByText('Coverage'))
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
            proxy_for_symbol: null, unweighted_constituents: 0,
          },
        ],
      }),
    )
    withProviders(<LookThroughTab />)

    await screen.findByText('Coverage')
    expect(coverageCard().querySelector('.text-green-600')).toBeNull()
    expect(coverageCard().querySelector('.text-yellow-600')).toBeTruthy()
    expect(screen.getByText(/1 fund without holdings/)).toBeTruthy()
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
            proxy_for_symbol: null, unweighted_constituents: 0,
          },
          {
            symbol: 'DBPG', fund_isin: 'LU0411078552', market_value_eur: 2703,
            status: 'excluded',
            reason: 'Synthetic swap-based fund: the basket it publishes is collateral.',
            basket_as_of: null, stale: false, constituents: 0, equity_weight_pct: null,
            residual_eur: 0, asset_class_available: true, source: null,
            proxy_for_symbol: null, unweighted_constituents: 0,
          },
        ],
      }),
    )
    withProviders(<LookThroughTab />)

    await screen.findByText('Coverage')
    expect(coverageCard().querySelector('.text-green-600')).toBeTruthy()
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
            proxy_for_symbol: null, unweighted_constituents: 0,
          },
        ],
      }),
    )
    withProviders(<LookThroughTab />)

    await screen.findByText('Coverage')
    // Every fund IS decomposed, so the card is not amber - but a hand-imported basket rots
    // invisibly, and the percentage never moves when it does.
    expect(coverageCard().querySelector('.text-green-600')).toBeTruthy()
    expect(screen.getByText(/1 fund with dated holdings/)).toBeTruthy()
  })

  it('is not green when a fund is decomposed from another fund\'s basket', async () => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(
      response({
        // Every fund reads `looked_through`, so the unresolved list is empty and the card
        // would have gone green on the old rule — while a tenth of the book was attributed
        // from a different fund's file. A borrowed basket is a real gap and it can be closed.
        coverage_pct: 96.8,
        funds: [
          {
            symbol: 'VWCE', fund_isin: 'IE00BK5BQT80', market_value_eur: 6510,
            status: 'looked_through', reason: null, basket_as_of: '2026-06-30', stale: false,
            constituents: 10032, equity_weight_pct: 91.86, residual_eur: 530,
            asset_class_available: false, source: 'vanguard_us',
            proxy_for_symbol: 'VT', unweighted_constituents: 8007,
          },
        ],
      }),
    )
    withProviders(<LookThroughTab />)

    await screen.findByText('Coverage')
    expect(coverageCard().querySelector('.text-green-600')).toBeNull()
    expect(screen.getByText(/1 fund via a similar fund/)).toBeTruthy()
    // And the fund's own row must say which, rather than a green "Decomposed".
    await openFundCoverage()
    expect(screen.getByText('Via VT')).toBeTruthy()
  })

  it('explains a broad fund\'s shortfall as rounding rather than leaving it to read as cash', async () => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(
      response({
        funds: [
          {
            symbol: 'VT', fund_isin: 'US9220427424', market_value_eur: 4000,
            status: 'looked_through', reason: null, basket_as_of: '2026-06-30', stale: false,
            constituents: 10032, equity_weight_pct: 91.86, residual_eur: 326,
            asset_class_available: false, source: 'vanguard_us',
            proxy_for_symbol: null, unweighted_constituents: 8007,
          },
        ],
      }),
    )
    withProviders(<LookThroughTab />)

    await screen.findByText('Coverage')
    await openFundCoverage()
    expect(screen.getByText('91.86%')).toBeTruthy()
    expect(screen.queryByText('8,007 rows at 0%')).toBeNull()
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
    // The treemap legend names each sector group it drew, plus the grey that keeps the areas
    // shares of the whole book. Alphabet is Communications and Nvidia Technology in the fixture.
    expect(screen.getAllByText('Technology').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Communications').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Not attributed to a company').length).toBeGreaterThan(0)
    // No sector is on the chart that no company belongs to.
    expect(screen.queryByText('Financials')).toBeNull()
  })

  it('keeps the composition bar vocabulary out of the treemap legend', async () => {
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(response())
    withProviders(<LookThroughTab />)

    await screen.findByText('Company exposure')
    // The bar splits value by how it is held; the treemap groups companies by sector. Sharing a
    // word between them would make one legend look like a key to the other chart.
    expect(screen.getAllByText('Held directly')).toHaveLength(1)
    expect(screen.getAllByText('Through funds')).toHaveLength(1)
  })

  it('draws no chart at all when nothing could be priced', async () => {
    // A zero-total book has an unknown shape. Rectangles drawn from it would be confident and
    // wrong, which is the `concentrationPct` refusal rather than a missing empty state.
    vi.spyOn(api, 'getLookthrough').mockResolvedValue(
      response({
        total_market_value_eur: 0,
        total_value_eur: 0,
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

describe('LookThroughTab Top N switch (GitHub #68)', () => {
  /**
   * Keyed on the limit with no placeholder, so pressing Top 100 blanked the tab to skeletons:
   * the pressed button left the DOM (focus fell to <body>) and the page collapsed under the
   * reader. The previous rows now stay, dimmed and busy, until the new ones arrive.
   */
  const companyRows = () => document.querySelectorAll('tbody tr').length
  const withMicrosoft = (over: Partial<LookthroughResponse> = {}) => {
    const base = response(over)
    return {
      ...base,
      companies: [
        ...base.companies,
        { ...base.companies[1], company_key: 'US5949181045', name: 'MICROSOFT CORP', isins: ['US5949181045'] },
      ],
    }
  }

  it('keeps the pressed button focused and the previous rows until the new ones arrive', async () => {
    let release: (r: LookthroughResponse) => void = () => {}
    const spy = vi.spyOn(api, 'getLookthrough').mockImplementation((limit) =>
      limit === 50 ? Promise.resolve(response()) : new Promise((resolve) => { release = resolve }),
    )
    withProviders(<LookThroughTab />)
    const top100 = await screen.findByRole('button', { name: 'Top 100' })
    await waitFor(() => expect(companyRows()).toBe(2))

    top100.focus()
    fireEvent.click(top100)
    await waitFor(() => expect(spy).toHaveBeenCalledWith(100))

    expect(top100.isConnected).toBe(true)
    expect(document.activeElement).toBe(top100)
    expect(companyRows()).toBe(2)
    expect(document.querySelector('[aria-busy="true"]')).not.toBeNull()

    await act(async () => release(withMicrosoft()))
    await screen.findAllByText('Microsoft')
    expect(companyRows()).toBe(3)
    expect(document.querySelector('[aria-busy]')).toBeNull()
    expect(document.activeElement).toBe(top100)
  })
})

describe('LookThroughTab base-currency switch (GitHub #22 under #68)', () => {
  /**
   * Keeping the previous rows must never carry them across a currency switch: EUR figures
   * under the CHF label is #22 again. Two routes reach it. The plain one (rows on screen,
   * then the switch) is closed by the reset alone; the second (a Top N fetch still in
   * flight, so a placeholder is ALREADY showing) is reused by TanStack without asking the
   * placeholder function again unless that function changes with the currency.
   */
  let serverBase = 'EUR'
  const settings = (base: string) => ({
    base_currency: base,
    supported_currencies: ['EUR', 'CHF', 'USD'],
    dividend_forecast_withholding_pct: 15,
  })
  const companyRows = () => document.querySelectorAll('tbody tr').length

  function CurrencySwitch() {
    const { baseCurrency, setBaseCurrency } = useBaseCurrency()
    return (
      <>
        <p data-testid="label">{baseCurrency}</p>
        <button onClick={() => setBaseCurrency('CHF')}>to CHF</button>
      </>
    )
  }

  beforeEach(() => {
    serverBase = 'EUR'
    vi.spyOn(api, 'getSettings').mockImplementation(async () => settings(serverBase) as never)
    vi.spyOn(api, 'updateBaseCurrency').mockImplementation(async () => {
      serverBase = 'CHF'
      return settings('CHF') as never
    })
  })

  /** The first request answers in EUR; every later one waits until the test releases it. */
  function mockLookthrough() {
    let calls = 0
    let release: () => void = () => {}
    const spy = vi.spyOn(api, 'getLookthrough').mockImplementation(() => {
      calls += 1
      const answer = response({ base_currency: serverBase })
      return calls === 1
        ? Promise.resolve(answer)
        : new Promise((resolve) => { release = () => resolve(answer) })
    })
    return { spy, release: () => release() }
  }

  it('blanks the figures instead of relabelling them', async () => {
    const { release } = mockLookthrough()
    withProviders(<><LookThroughTab /><CurrencySwitch /></>)
    await waitFor(() => expect(companyRows()).toBe(2))
    await waitFor(() => expect(screen.getByTestId('label').textContent).toBe('EUR'))

    fireEvent.click(screen.getByText('to CHF'))
    await waitFor(() => expect(screen.getByTestId('label').textContent).toBe('CHF'))
    expect(companyRows()).toBe(0)
    expect(screen.queryByRole('button', { name: 'Top 100' })).toBeNull()

    await act(async () => release())
    await waitFor(() => expect(companyRows()).toBe(2))
  })

  it('blanks them too when the switch lands while another Top N is loading', async () => {
    const { spy, release } = mockLookthrough()
    withProviders(<><LookThroughTab /><CurrencySwitch /></>)
    await waitFor(() => expect(companyRows()).toBe(2))
    await waitFor(() => expect(screen.getByTestId('label').textContent).toBe('EUR'))

    fireEvent.click(screen.getByRole('button', { name: 'Top 100' }))
    await waitFor(() => expect(spy).toHaveBeenCalledWith(100))
    expect(companyRows()).toBe(2) // the EUR rows, kept while Top 100 loads

    fireEvent.click(screen.getByText('to CHF'))
    await waitFor(() => expect(screen.getByTestId('label').textContent).toBe('CHF'))
    expect(companyRows()).toBe(0)

    await act(async () => release())
    await waitFor(() => expect(companyRows()).toBe(2))
  })
})
