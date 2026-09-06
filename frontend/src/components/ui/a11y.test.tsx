// @vitest-environment jsdom
import { describe, it, expect, afterEach, vi } from 'vitest'
import { useState } from 'react'
import { render, screen, cleanup } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Card, CardContent } from './card'
import { CollapsibleCardHeader } from './CollapsibleCardHeader'
import { SortableTh } from './SortableTh'

/**
 * Four cards put onClick on a <CardHeader> div and two tables put it on a <th>, so
 * Monthly Returns, Money In per Month, Dividend Income, Performance Attribution and
 * every sortable column in Fundamentals and Watchlist were mouse-only. These pin the
 * shared components that replaced them.
 */

afterEach(cleanup)

function Collapsible() {
  const [open, setOpen] = useState(false)
  return (
    <Card>
      <CollapsibleCardHeader
        open={open}
        onToggle={() => setOpen(o => !o)}
        title="Monthly Returns"
        description="Summary line"
        contentId="test-content"
      />
      {open && <CardContent id="test-content">Body</CardContent>}
    </Card>
  )
}

describe('CollapsibleCardHeader', () => {
  it('is a button, so the browser gives it focus and key handling for free', () => {
    render(<Collapsible />)
    expect(screen.getByRole('button', { name: /Monthly Returns/ })).toBeTruthy()
  })

  it('announces its state and what it controls', async () => {
    const user = userEvent.setup()
    render(<Collapsible />)
    const button = screen.getByRole('button', { name: /Monthly Returns/ })

    expect(button.getAttribute('aria-expanded')).toBe('false')
    expect(button.getAttribute('aria-controls')).toBe('test-content')

    await user.click(button)
    expect(button.getAttribute('aria-expanded')).toBe('true')
    // The id it names must actually exist once expanded, or the reference dangles.
    expect(document.getElementById('test-content')).toBeTruthy()
  })

  it('opens on Enter and on Space', async () => {
    const user = userEvent.setup()
    render(<Collapsible />)
    const button = screen.getByRole('button', { name: /Monthly Returns/ })

    button.focus()
    await user.keyboard('{Enter}')
    expect(button.getAttribute('aria-expanded')).toBe('true')

    await user.keyboard(' ')
    expect(button.getAttribute('aria-expanded')).toBe('false')
  })

  it('is reachable by Tab', async () => {
    const user = userEvent.setup()
    render(<Collapsible />)

    await user.tab()
    expect(document.activeElement).toBe(screen.getByRole('button', { name: /Monthly Returns/ }))
  })

  it('keeps the description visible in both states', async () => {
    const user = userEvent.setup()
    render(<Collapsible />)
    expect(screen.getByText('Summary line')).toBeTruthy()

    await user.click(screen.getByRole('button', { name: /Monthly Returns/ }))
    expect(screen.getByText('Summary line')).toBeTruthy()
  })
})

type Col = 'symbol' | 'value'

function Table({ onSort = () => {} }: { onSort?: (c: Col) => void }) {
  return (
    <table>
      <thead>
        <tr>
          <SortableTh
            column="symbol"
            label="Symbol"
            activeColumn="symbol"
            direction="asc"
            onSort={onSort}
          />
          <SortableTh
            column="value"
            label="Value"
            activeColumn="symbol"
            direction="asc"
            onSort={onSort}
            align="right"
          />
        </tr>
      </thead>
      <tbody><tr><td>a</td><td>1</td></tr></tbody>
    </table>
  )
}

describe('SortableTh', () => {
  it('reports the sort state on the header cell', () => {
    render(<Table />)
    const [sorted, unsorted] = screen.getAllByRole('columnheader')

    // Without aria-sort the arrow glyph is decorative and nothing announces the order.
    expect(sorted.getAttribute('aria-sort')).toBe('ascending')
    expect(unsorted.getAttribute('aria-sort')).toBe('none')
  })

  it('flips to descending when told', () => {
    render(
      <table><thead><tr>
        <SortableTh column="symbol" label="Symbol" activeColumn="symbol"
                    direction="desc" onSort={() => {}} />
      </tr></thead></table>
    )
    expect(screen.getByRole('columnheader').getAttribute('aria-sort')).toBe('descending')
  })

  it('sorts from the keyboard', async () => {
    const user = userEvent.setup()
    const onSort = vi.fn()
    render(<Table onSort={onSort} />)

    await user.tab()
    await user.keyboard('{Enter}')
    expect(onSort).toHaveBeenCalledWith('symbol')

    await user.tab()
    await user.keyboard('{Enter}')
    expect(onSort).toHaveBeenCalledWith('value')
  })

  it('still sorts on click', async () => {
    const user = userEvent.setup()
    const onSort = vi.fn()
    render(<Table onSort={onSort} />)

    await user.click(screen.getByRole('button', { name: /Value/ }))
    expect(onSort).toHaveBeenCalledWith('value')
  })

  it('hides the direction icon from the accessibility tree', () => {
    // aria-sort already carries the meaning; the glyph would repeat it as noise.
    render(<Table />)
    const button = screen.getByRole('button', { name: 'Symbol' })
    expect(button.textContent).toBe('Symbol')
  })
})
