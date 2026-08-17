// @vitest-environment jsdom
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { render, screen, cleanup, fireEvent, within } from '@testing-library/react'
import { DataTable, type Column, type FooterCell } from './DataTable'

/**
 * The point of this file is the anti-drift assertion, not the markup.
 *
 * A desktop table and a phone card list rendering the same rows is the shape
 * CLAUDE.md's "two implementations of one job" section is about, and the failure is
 * invisible: a column missing from the phone produces nothing at all, on a device the
 * author is not looking at. So every test here drives BOTH modes from ONE fixture and
 * ONE `columns` array via the `mode` seam, and `every column reaches both renderings`
 * below is the one that actually holds the line.
 */

afterEach(cleanup)

/**
 * `ScrollableTable` measures overflow with a ResizeObserver, which jsdom does not
 * implement. It has been in every browser since 2020, so this is a gap in the test
 * environment rather than something the component should guard against.
 */
beforeEach(() => {
  if (!('ResizeObserver' in globalThis)) {
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver
  }
})

interface Holding {
  id: number
  symbol: string
  name: string
  value: number
  changePct: number
  qty: number
  cost: number
  weight: number
}

const ROWS: Holding[] = [
  { id: 1, symbol: 'AAPL', name: 'Apple Inc', value: 4120.55, changePct: 12.3, qty: 12, cost: 3668.2, weight: 6.6 },
  { id: 2, symbol: 'ASML', name: 'ASML Holding NV', value: 3890.1, changePct: -2.1, qty: 4, cost: 3973.6, weight: 6.2 },
]

type SortKey = 'symbol' | 'value'

const tone = (r: Holding) => (r.changePct >= 0 ? 'text-green-600' : 'text-red-600')

const COLUMNS: Column<Holding, SortKey>[] = [
  { key: 'symbol', header: 'Symbol', sortKey: 'symbol', shortHeader: 'Symbol', mobile: 'title', cell: r => r.symbol },
  { key: 'name', header: 'Description', mobile: 'meta', cell: r => r.name },
  {
    key: 'value',
    header: 'Market Value',
    shortHeader: 'Value',
    sortKey: 'value',
    align: 'right',
    mobile: 'value',
    cell: r => r.value.toFixed(2),
  },
  { key: 'change', header: '%', align: 'right', mobile: 'delta', tone, cell: r => `${r.changePct}%` },
  { key: 'qty', header: 'Quantity', align: 'right', cell: r => r.qty },
  { key: 'cost', header: 'Cost Basis', align: 'right', cell: r => r.cost.toFixed(2) },
  {
    key: 'weight',
    header: 'Weight',
    align: 'right',
    hint: { description: 'Share of total portfolio market value.', formula: 'value / total' },
    cell: r => `${r.weight}%`,
  },
]

const FOOTER: FooterCell[] = [
  { key: 'label', spans: ['symbol', 'name'], content: 'Total' },
  { key: 'value', spans: ['value'], content: '8010.65', align: 'right' },
]

function renderBoth(extra: Partial<React.ComponentProps<typeof DataTable<Holding, SortKey>>> = {}) {
  const base = {
    rows: ROWS,
    columns: COLUMNS,
    getRowKey: (r: Holding) => r.id,
    label: 'Holdings table',
    ...extra,
  }
  // Both stay mounted, in their own containers: cleaning up between them would empty
  // the first before anything could be asserted against it.
  const { container: tableView } = render(<DataTable {...base} mode="table" />)
  const { container: cardView } = render(<DataTable {...base} mode="cards" />)
  return { tableView, cardView }
}

describe('DataTable renders one description two ways', () => {
  it('every column reaches both renderings', () => {
    const { tableView, cardView } = renderBoth()

    for (const col of COLUMNS) {
      for (const row of ROWS) {
        if (col.desktop !== 'hide') {
          const text = String(col.cell(row, 'table'))
          expect(tableView.textContent, `${col.key} missing from the table`).toContain(text)
        }
        if ((col.mobile ?? 'detail') !== 'hide') {
          const text = String(col.cell(row, 'cards'))
          expect(cardView.textContent, `${col.key} missing from the cards`).toContain(text)
        }
      }
    }
  })

  it('renders the same number of rows either way', () => {
    const { tableView, cardView } = renderBoth()
    // header row + one per holding
    expect(tableView.querySelectorAll('tr')).toHaveLength(ROWS.length + 1)
    expect(cardView.querySelectorAll('li')).toHaveLength(ROWS.length)
  })

  it('emits no table markup at all in card mode', () => {
    const { cardView } = renderBoth()
    expect(cardView.querySelector('table')).toBeNull()
    expect(cardView.querySelector('ul')?.getAttribute('aria-label')).toBe('Holdings table')
  })

  it('applies tone in both modes, so a loss is never monochrome on a phone', () => {
    // The green/red currently lives in `<td>` class strings beside the layout classes.
    // If it is not lifted into `tone`, the cards render every delta in body colour —
    // a silent, plausible-looking wrong reading.
    const { tableView, cardView } = renderBoth()
    expect(tableView.querySelector('.text-red-600')?.textContent).toContain('-2.1%')
    expect(cardView.querySelector('.text-red-600')?.textContent).toContain('-2.1%')
  })

  it('keeps a column out of the cards only when it says so', () => {
    const columns: Column<Holding, SortKey>[] = [
      ...COLUMNS,
      { key: 'isin', header: 'ISIN', mobile: 'hide', cell: () => 'US0378331005' },
      { key: 'gross', header: 'Gross', desktop: 'hide', cell: () => 'GROSSVALUE' },
    ]
    const { tableView, cardView } = renderBoth({ columns })
    expect(tableView.textContent).toContain('US0378331005')
    expect(cardView.textContent).not.toContain('US0378331005')
    expect(tableView.textContent).not.toContain('GROSSVALUE')
    expect(cardView.textContent).toContain('GROSSVALUE')
  })
})

describe('sorting survives the switch', () => {
  const sort = { column: 'value' as SortKey, direction: 'desc' as const, onSort: vi.fn() }

  it('emits aria-sort on every sortable header, which e2e/a11y counts', () => {
    render(<DataTable rows={ROWS} columns={COLUMNS} getRowKey={r => r.id} label="t" sort={sort} mode="table" />)
    const sorted = document.querySelectorAll('th[aria-sort]')
    expect(sorted).toHaveLength(2)
    expect(document.querySelector('th[aria-sort="descending"]')?.textContent).toContain('Market Value')
  })

  it('offers exactly the sortable columns in the phone control, and toggles direction', () => {
    const onSort = vi.fn()
    render(
      <DataTable
        rows={ROWS}
        columns={COLUMNS}
        getRowKey={r => r.id}
        label="Holdings table"
        sort={{ ...sort, onSort }}
        mode="cards"
      />,
    )
    const select = screen.getByLabelText('Sort Holdings table by') as HTMLSelectElement
    expect(within(select).getAllByRole('option')).toHaveLength(2)

    fireEvent.change(select, { target: { value: 'symbol' } })
    expect(onSort).toHaveBeenCalledWith('symbol')

    // The direction button re-fires the ACTIVE column — the contract every table in
    // this app already implements, and what makes one button enough.
    fireEvent.click(screen.getByLabelText('Sort ascending'))
    expect(onSort).toHaveBeenCalledWith('value')
  })

  it('shows no sort control when the caller passes no sort state', () => {
    render(<DataTable rows={ROWS} columns={COLUMNS} getRowKey={r => r.id} label="t" mode="cards" />)
    expect(screen.queryByRole('combobox')).toBeNull()
  })
})

describe('the awkward shapes', () => {
  it('renders a row with neither value nor delta without an empty figure block', () => {
    // ActivityTab's deposit shape: cash flows have no symbol and no price.
    const columns: Column<Holding>[] = [
      { key: 'symbol', header: 'Symbol', mobile: 'title', cell: r => r.symbol },
      { key: 'qty', header: 'Quantity', cell: r => r.qty },
    ]
    render(<DataTable rows={ROWS} columns={columns} getRowKey={r => r.id} label="t" mode="cards" />)
    const first = document.querySelector('li')!
    expect(first.querySelector('.tabular-nums.text-right')).toBeNull()
    expect(first.textContent).toContain('AAPL')
  })

  it('renders the footer in both modes with the same figures, and spans the right width', () => {
    const { tableView, cardView } = renderBoth({ footer: FOOTER })

    const cell = tableView.querySelector('tfoot td')!
    expect(cell.getAttribute('colspan')).toBe('2')
    expect(tableView.querySelector('tfoot')?.textContent).toContain('8010.65')
    expect(cardView.textContent).toContain('8010.65')
    // The single-column footer cell derives its phone label from the column it spans.
    expect(cardView.querySelector('li:last-child')?.textContent).toContain('Value')
  })

  it('pads a partially-spanned footer out to the full table width', () => {
    // What the tax tables' trailing empty <td> was, derived instead of written out.
    const { tableView } = renderBoth({ footer: FOOTER })
    const cells = tableView.querySelectorAll('tfoot td')
    const spanned = Array.from(cells).reduce((n, c) => n + Number(c.getAttribute('colspan') ?? 1), 0)
    expect(spanned).toBe(COLUMNS.length)
  })

  it('discloses the details beyond detailLimit', () => {
    render(
      <DataTable rows={ROWS} columns={COLUMNS} getRowKey={r => r.id} label="t" detailLimit={1} mode="cards" />,
    )
    const first = document.querySelector('li')!
    // qty, cost and weight are details; only the first is shown.
    expect(within(first as HTMLElement).queryByText('Cost Basis')).toBeNull()
    fireEvent.click(within(first as HTMLElement).getByText('Show all 3 metrics'))
    expect(within(first as HTMLElement).getByText('Cost Basis')).toBeTruthy()
  })

  it('renders rowActions in both modes from one render function', () => {
    const actions = (r: Holding) => <button type="button">Remove {r.symbol}</button>
    const { tableView, cardView } = renderBoth({ rowActions: actions })
    expect(tableView.textContent).toContain('Remove AAPL')
    expect(cardView.textContent).toContain('Remove AAPL')
  })
})

/**
 * `expandedRow` exists because there was nowhere to put a per-row panel: the Look-through
 * tab's company breakdown had to be rendered *outside* the table, which put it below every
 * row and both footnotes — one to three screens from the button that opened it, so the
 * button looked inert.
 *
 * The assertions that matter are about *placement*, not presence. A panel rendered anywhere
 * in the document satisfies "is the text there", which is exactly how the original bug
 * passed every check that existed.
 */
describe('expandedRow puts a panel under its own row', () => {
  const expansion = (r: Holding) =>
    r.symbol === 'ASML' ? <p>Inside {r.symbol}</p> : null

  it('renders in both modes from one render function', () => {
    const { tableView, cardView } = renderBoth({ expandedRow: expansion })
    expect(tableView.textContent).toContain('Inside ASML')
    expect(cardView.textContent).toContain('Inside ASML')
  })

  it('renders only for the rows that return content', () => {
    const { tableView, cardView } = renderBoth({ expandedRow: expansion })
    expect(tableView.textContent).not.toContain('Inside AAPL')
    expect(cardView.textContent).not.toContain('Inside AAPL')
    // One extra <tr> beyond header + rows, not one per row.
    expect(tableView.querySelectorAll('tr')).toHaveLength(ROWS.length + 2)
  })

  it('places the panel IMMEDIATELY after its own row, not after the table', () => {
    // The whole point. Asserting only that the text exists passes against a panel
    // rendered below every row, which is the bug this slot was added to fix.
    const { tableView } = renderBoth({ expandedRow: expansion })
    const bodyRows = Array.from(tableView.querySelectorAll('tbody tr'))
    const owner = bodyRows.findIndex(tr => tr.textContent?.includes('ASML'))
    expect(owner).toBeGreaterThanOrEqual(0)
    expect(bodyRows[owner + 1]?.textContent).toContain('Inside ASML')
  })

  it('places it inside its own card on a phone, not after the list', () => {
    const { cardView } = renderBoth({ expandedRow: expansion })
    const items = Array.from(cardView.querySelectorAll('li'))
    const owner = items.find(li => li.textContent?.includes('ASML Holding NV'))
    expect(owner?.textContent).toContain('Inside ASML')
    // ...and not in a sibling card, which is the same misplacement one level down.
    const others = items.filter(li => li !== owner)
    expect(others.some(li => li.textContent?.includes('Inside ASML'))).toBe(false)
  })

  it('spans every column, counting the rowActions one', () => {
    // A hardcoded colSpan is what the footer code records as lying silently once a column
    // is added, so this pins the span against the real column count.
    const actions = () => <button type="button">Go</button>
    const { tableView } = renderBoth({ expandedRow: expansion, rowActions: actions })
    const visible = COLUMNS.filter(c => c.desktop !== 'hide').length
    const cell = tableView.querySelector('tbody tr td[colspan]')
    expect(cell?.getAttribute('colspan')).toBe(String(visible + 1))
  })

  it('changes nothing when the caller passes no expandedRow', () => {
    const { tableView, cardView } = renderBoth()
    expect(tableView.querySelector('td[colspan]')).toBeNull()
    expect(tableView.querySelectorAll('tr')).toHaveLength(ROWS.length + 1)
    expect(cardView.querySelectorAll('li')).toHaveLength(ROWS.length)
  })
})

describe('the hint glossary', () => {
  it('lists every hinted column, in both modes, from one source', () => {
    // The desktop `title=` and the phone glossary read the same `hint`, so a
    // definition cannot exist on one surface and be missing from the other.
    for (const mode of ['table', 'cards'] as const) {
      cleanup()
      render(<DataTable rows={ROWS} columns={COLUMNS} getRowKey={r => r.id} label="Holdings table" mode={mode} />)
      fireEvent.click(screen.getByLabelText('What the Holdings table columns mean'))
      expect(screen.getByText('Share of total portfolio market value.')).toBeTruthy()
      expect(screen.getByText('value / total')).toBeTruthy()
    }
  })

  it('also puts the hint on the desktop header, where it was before', () => {
    render(<DataTable rows={ROWS} columns={COLUMNS} getRowKey={r => r.id} label="t" mode="table" />)
    const th = Array.from(document.querySelectorAll('th')).find(h => h.textContent?.includes('Weight'))
    expect(th?.getAttribute('title')).toBe('Share of total portfolio market value.')
  })

  it('renders no glossary button when no column defines a hint', () => {
    const columns = COLUMNS.map(c => ({ ...c, hint: undefined }))
    render(<DataTable rows={ROWS} columns={columns} getRowKey={r => r.id} label="t" mode="cards" />)
    expect(screen.queryByLabelText(/columns mean/)).toBeNull()
  })
})
