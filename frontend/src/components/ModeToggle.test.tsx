// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { ModeToggle } from './ModeToggle'
import { MODE_STORAGE_KEY, ModeProvider, useMode } from '@/lib/portfolioMode'

afterEach(() => {
  cleanup()
  localStorage.clear()
})

function Shown() {
  return <span data-testid="shown">{useMode().mode}</span>
}

function renderToggle() {
  return render(
    <ModeProvider>
      <ModeToggle />
      <Shown />
    </ModeProvider>,
  )
}

describe('ModeToggle', () => {
  it('is a labelled group of two pressed-state buttons', () => {
    renderToggle()
    const group = screen.getByRole('group', { name: 'Portfolio' })
    const stocks = screen.getByRole('button', { name: 'Stocks' })
    const crypto = screen.getByRole('button', { name: 'Crypto' })
    expect(group.contains(stocks) && group.contains(crypto)).toBe(true)
    expect(stocks.getAttribute('aria-pressed')).toBe('true')
    expect(crypto.getAttribute('aria-pressed')).toBe('false')
  })

  it('is not a tab strip — the page keeps exactly one tablist', () => {
    renderToggle()
    expect(screen.queryAllByRole('tablist')).toHaveLength(0)
    expect(screen.queryAllByRole('tab')).toHaveLength(0)
  })

  it('switches the book and remembers it', () => {
    renderToggle()
    fireEvent.click(screen.getByRole('button', { name: 'Crypto' }))
    expect(screen.getByTestId('shown').textContent).toBe('crypto')
    expect(screen.getByRole('button', { name: 'Crypto' }).getAttribute('aria-pressed')).toBe('true')
    expect(localStorage.getItem(MODE_STORAGE_KEY)).toBe('crypto')
  })
})
