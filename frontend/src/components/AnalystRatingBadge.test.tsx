// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { AnalystRatingBadge, AnalystRatingBar } from './AnalystRatingBadge'
import { ratingCountsText } from '@/lib/analystRating'

afterEach(cleanup)

const RATING = { strong_buy: 25, buy: 10, hold: 3, sell: 1, strong_sell: 0, consensus: 'Strong Buy' }

describe('AnalystRatingBadge', () => {
  it('shows the consensus, a bar of the counts and the counts on hover', () => {
    const { container } = render(<AnalystRatingBadge rating={RATING} />)

    expect(screen.getByText('Strong Buy')).toBeTruthy()
    expect(container.firstElementChild?.getAttribute('title')).toBe(
      '25 Strong Buy · 10 Buy · 3 Hold · 1 Sell',
    )
    const bar = screen.getByRole('img')
    // One segment per non-zero count, sized by it, in Strong Buy → Strong Sell order.
    const widths = Array.from(bar.children).map((s) => (s as HTMLElement).style.flex)
    expect(widths).toEqual(['25 1 0%', '10 1 0%', '3 1 0%', '1 1 0%'])
    expect(bar.getAttribute('aria-label')).toBe('25 Strong Buy · 10 Buy · 3 Hold · 1 Sell')
  })

  it('draws no bar when there are no counts', () => {
    const { container } = render(
      <AnalystRatingBar counts={{ strong_buy: 0, buy: 0, hold: 0, sell: 0, strong_sell: 0 }} />,
    )
    expect(container.innerHTML).toBe('')
  })

  it('writes the counts that are not zero, strongest first', () => {
    expect(ratingCountsText({ strong_buy: 0, buy: 2, hold: 0, sell: 0, strong_sell: 3 })).toBe(
      '2 Buy · 3 Strong Sell',
    )
  })
})
