// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { LookthroughFundamentalsCard } from './LookthroughFundamentalsCard'
import { api, type LookthroughFundamentals } from '@/lib/api'

const BODY: LookthroughFundamentals = {
  as_of: '2026-11-08', pe_ratio: 24.6, revenue_growth: 0.123, earnings_growth: -0.05,
  operating_margin: 0.31, fcf_margin: null, coverage_pct: 68.4, companies_covered: 61,
  companies: 2400,
}

function renderCard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <LookthroughFundamentalsCard />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  window.localStorage.clear()
})

describe('LookthroughFundamentalsCard (review item R2)', () => {
  it('asks the server only once it is opened, then shows the figures', async () => {
    const get = vi.spyOn(api, 'getLookthroughFundamentals').mockResolvedValue(BODY)
    renderCard()
    expect(get).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: /Fundamentals of what you own/ }))
    const card = await screen.findByTestId('lookthrough-fundamentals')
    expect(await screen.findByText('24.6')).toBeTruthy()
    expect(card.textContent).toContain('+12.3%')
    expect(card.textContent).toContain('-5.0%')
    expect(card.textContent).toContain('31.0%')
    expect(card.textContent).toContain('68%')
    expect(card.textContent).toContain('61 of 2,400 companies')
    expect(get).toHaveBeenCalledTimes(1)
  })
})
