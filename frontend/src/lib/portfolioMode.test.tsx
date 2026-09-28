// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
import { MODE_STORAGE_KEY, ModeProvider, readStoredMode, useMode } from './portfolioMode'

afterEach(() => {
  cleanup()
  localStorage.clear()
})

function Probe() {
  const { mode, setMode } = useMode()
  return (
    <>
      <span data-testid="mode">{mode}</span>
      <button type="button" onClick={() => setMode('crypto')}>crypto</button>
    </>
  )
}

describe('the portfolio mode', () => {
  it('starts on stocks when nothing is stored', () => {
    expect(readStoredMode()).toBe('stocks')
  })

  it('reads a stored crypto choice back', () => {
    localStorage.setItem(MODE_STORAGE_KEY, 'crypto')
    expect(readStoredMode()).toBe('crypto')
  })

  it('treats anything else as stocks rather than rendering nothing', () => {
    // An old build's value or a hand edit must not leave the page without a view.
    for (const junk of ['Crypto', 'bonds', '', '{"mode":"crypto"}']) {
      localStorage.setItem(MODE_STORAGE_KEY, junk)
      expect(readStoredMode(), junk).toBe('stocks')
    }
  })

  it('persists a switch so a reload comes back to the same book', () => {
    render(<ModeProvider><Probe /></ModeProvider>)
    expect(screen.getByTestId('mode').textContent).toBe('stocks')
    act(() => screen.getByText('crypto').click())
    expect(screen.getByTestId('mode').textContent).toBe('crypto')
    expect(localStorage.getItem(MODE_STORAGE_KEY)).toBe('crypto')
  })
})
