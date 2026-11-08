// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render } from '@testing-library/react'
import { metricsColumns } from './FundamentalsTab'
import { watchlistColumns } from './watchlistColumns'

/**
 * Review item Y1: Yahoo's "TTM" growth is the latest quarter against the same one a year
 * before. Where the server could not serve the filings' TTM it says so
 * (`growth_basis: 'quarter'`), and the figure carries a `q`.
 */

afterEach(cleanup)

const cellText = (cell: React.ReactNode) => render(<>{cell}</>).container.textContent

describe("a growth figure says when it is a quarter's", () => {
  it('on Fundamentals', () => {
    const col = metricsColumns('$').find((c) => c.key === 'revenue_growth')!
    const row = (basis: 'ttm' | 'quarter') =>
      ({ revenue_growth: 0.25, growth_basis: basis }) as never
    expect(cellText(col.cell(row('quarter'), 'table'))).toMatch(/q$/)
    expect(cellText(col.cell(row('ttm'), 'table'))).not.toMatch(/q$/)
  })

  it('on the watchlist', () => {
    const col = watchlistColumns({ formatCurrency: () => '', currencySymbol: '$' })
      .find((c) => c.key === 'earnings_growth')!
    expect(cellText(col.cell({ earnings_growth: 0.5, growth_basis: 'quarter' } as never, 'table')))
      .toBe('50.0%q')
    expect(cellText(col.cell({ earnings_growth: 0.5, growth_basis: 'ttm' } as never, 'table')))
      .toBe('50.0%')
  })
})
