// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { Wallet } from 'lucide-react'
import { KpiCard, KpiCardSkeleton, KpiPanel } from './KpiCard'
import { ABSENT } from '@/lib/utils'

// Explicit, because there is no vitest setup file — jsdom is opted into per file, so
// testing-library's auto-cleanup is never registered and renders otherwise accumulate
// across tests in the same file. Same as every other component spec here.
afterEach(cleanup)

/**
 * `KpiCard` replaced sixteen hand-written copies of one card across three files. The
 * per-file specs (e.g. `RiskMetricsCards.test.tsx`) still assert what each card *says*;
 * this pins the primitive's own contract, and the conventions the callers rely on.
 */
describe('KpiCard', () => {
  it('renders the label, the value and the footnote', () => {
    render(<KpiCard label="Sharpe Ratio" value="1.42" sub="Risk-adjusted return" />)
    expect(screen.getByText('Sharpe Ratio')).toBeTruthy()
    expect(screen.getByText('1.42')).toBeTruthy()
    expect(screen.getByText('Risk-adjusted return')).toBeTruthy()
  })

  it('keeps a figure on one line, and six tiles in a row only where they fit', () => {
    // "-CHF 57,244.07" broke after its ASCII hyphen in a six-tile strip at 1280px: the
    // sign alone on one line, the amount on the next.
    const { container } = render(
      <KpiPanel columns={6}><KpiCard tile label="Bought" value="-CHF 57,244.07" /></KpiPanel>,
    )
    expect(screen.getByText('-CHF 57,244.07').className).toContain('whitespace-nowrap')
    const grid = container.querySelector('.grid') as HTMLElement
    expect(grid.className).toContain('xl:grid-cols-6')
    expect(grid.className).not.toContain('lg:grid-cols-6')
  })

  it('renders an absent value as a dash, never a zero', () => {
    // The invariant three of the four files each implemented separately — and disagreed
    // on. An absent metric means "unknown"; rendering 0 would assert a fact.
    render(<KpiCard label="Beta" value={null} sub="Pick a benchmark" />)
    expect(screen.getByText(ABSENT)).toBeTruthy()
    expect(screen.queryByText('0')).toBeNull()
  })

  it('treats undefined the same as null', () => {
    render(<KpiCard label="Sortino" value={undefined} />)
    expect(screen.getByText(ABSENT)).toBeTruthy()
  })

  it('keeps a real zero, which is a fact rather than an absence', () => {
    // `Positions: 0` and `Max Drawdown: 0.00%` are true statements. Collapsing them into
    // the dash would be the mirror-image bug.
    render(<KpiCard label="Positions" value={0} />)
    expect(screen.getByText('0')).toBeTruthy()
    expect(screen.queryByText(ABSENT)).toBeNull()
  })

  it('refuses to colour an absent value as good or bad news', () => {
    // A caller computing `tone` from a number it has not null-checked would otherwise
    // paint the dash green.
    const { container } = render(<KpiCard label="Calmar" value={null} tone="positive" />)
    const value = screen.getByText(ABSENT)
    expect(value.className).toContain('text-muted-foreground')
    expect(value.className).not.toContain('text-green-600')
    expect(container.querySelectorAll('.text-green-600').length).toBe(0)
  })

  it('maps each tone to exactly one colour', () => {
    const cases = [
      ['positive', 'text-green-600'],
      ['negative', 'text-red-600'],
      ['warning', 'text-yellow-600'],
      ['muted', 'text-muted-foreground'],
    ] as const
    for (const [tone, cls] of cases) {
      const { unmount } = render(<KpiCard label="x" value="1" tone={tone} />)
      expect(screen.getByText('1').className, tone).toContain(cls)
      unmount()
    }
  })

  it('gives a neutral value no colour class at all', () => {
    render(<KpiCard label="Win Rate" value="61.0%" />)
    const cls = screen.getByText('61.0%').className
    for (const c of ['text-green-600', 'text-red-600', 'text-yellow-600']) {
      expect(cls).not.toContain(c)
    }
  })

  it('shrinks the value on a phone but not the hero', () => {
    // A 2-up cell has a ~141px interior and a seven-figure currency string renders at
    // ~182px in text-2xl, so `text-lg` below `sm` is load-bearing. The hero spans both
    // columns, so it keeps the large size.
    const { unmount } = render(<KpiCard label="Cost Basis" value="CHF 48,561" />)
    expect(screen.getByText('CHF 48,561').className).toContain('text-lg')
    expect(screen.getByText('CHF 48,561').className).toContain('sm:text-[1.375rem]')
    unmount()

    render(<KpiCard hero label="Market Value" value="CHF 65,024" />)
    const hero = screen.getByText('CHF 65,024')
    expect(hero.className).toContain('text-[1.75rem]')
    expect(hero.className).not.toContain('text-lg')
  })

  it('spans both phone columns only for the hero', () => {
    const { container, unmount } = render(<KpiCard hero label="a" value="1" />)
    expect(container.querySelector('.col-span-2')).toBeTruthy()
    unmount()

    const plain = render(<KpiCard label="a" value="1" />)
    expect(plain.container.querySelector('.col-span-2')).toBeNull()
  })

  it('renders footer content raw, so a chip keeps its own colours', () => {
    // The reason `footer` exists separately from `sub`: wrapping a DeltaChip in the muted
    // footnote line would fight its own styling.
    render(
      <KpiCard label="Market Value" value="1" footer={<span data-testid="chip">+3.4%</span>} />,
    )
    const chip = screen.getByTestId('chip')
    expect(chip.parentElement?.className ?? '').not.toContain('text-muted-foreground')
  })

  it('omits the footnote line entirely when there is nothing to say', () => {
    const { container } = render(<KpiCard label="a" value="1" />)
    expect(container.querySelector('p')).toBeNull()
  })

  it('renders one skeleton per requested slot', () => {
    const { container } = render(<KpiCardSkeleton count={6} />)
    expect(container.querySelectorAll('.animate-pulse').length).toBe(6)
  })

  it('renders tile KPIs as cells in one shared panel', () => {
    const { container } = render(
      <KpiPanel>
        <KpiCard tile label="Volatility" value="12.4%" />
        <KpiCard tile label="Sortino" value="1.42" />
      </KpiPanel>,
    )
    const panel = container.firstElementChild
    const grid = panel?.firstElementChild
    expect(panel?.className).toContain('rounded-2xl')
    expect(grid?.className).toContain('grid-cols-2')
    expect(grid?.children).toHaveLength(2)
    expect(grid?.children[0]?.className).toContain('p-4')
    expect(grid?.children[0]?.className).not.toContain('border')
  })
})

/**
 * The design-foundation props (2026-10-17). Every one is optional, so the block above —
 * written before they existed — is the backwards-compatibility suite; this pins what each
 * new prop adds and that it adds nothing when absent.
 */
describe('KpiCard furniture', () => {
  it('draws a lucide component in a tinted, decorative well, in both renderings', () => {
    for (const tile of [false, true]) {
      const { container, unmount } = render(<KpiCard tile={tile} icon={Wallet} label="Cash" value="CHF 46" />)
      const well = container.querySelector('span[aria-hidden="true"]')
      expect(well?.querySelector('svg'), `tile=${tile}`).toBeTruthy()
      expect(well?.className).toContain('bg-info-soft')
      // The label is still the name; the icon adds none.
      expect(screen.getByText('Cash')).toBeTruthy()
      unmount()
    }
  })

  it('tints the well with the tone, an override, or muted for an absent value', () => {
    const cases = [
      [{ tone: 'positive' as const }, 'bg-gain-soft'],
      [{ tone: 'negative' as const }, 'bg-loss-soft'],
      [{ tone: 'positive' as const, iconTone: 'warning' as const }, 'bg-warn-soft'],
    ] as const
    for (const [props, cls] of cases) {
      const { container, unmount } = render(<KpiCard icon={Wallet} label="x" value="1" {...props} />)
      expect(container.querySelector('span[aria-hidden="true"]')?.className).toContain(cls)
      unmount()
    }
    const { container } = render(<KpiCard icon={Wallet} label="x" value={null} tone="positive" />)
    expect(container.querySelector('span[aria-hidden="true"]')?.className).toContain('bg-muted')
  })

  it('keeps the older element form where it was: top right of a card, absent from a tile', () => {
    const icon = <svg data-testid="legacy" />
    const { unmount } = render(<KpiCard icon={icon} label="x" value="1" />)
    expect(screen.getByTestId('legacy').parentElement?.className).toContain('opacity-70')
    unmount()
    render(<KpiCard tile icon={icon} label="x" value="1" />)
    expect(screen.queryByTestId('legacy')).toBeNull()
  })

  it('renders a delta as a pill beside the value, coloured by any move at all', () => {
    // flatBand defaults to 0 here: a 2% move is signal on a KPI, where DeltaChip's own
    // default band (made for lumpy dividends) would have greyed it out.
    render(<KpiCard label="Value" value="CHF 82,092" delta={{ pct: 2, label: 'vs 1M' }} />)
    const chip = screen.getByText('+2%').parentElement
    expect(chip?.className).toContain('rounded-full')
    expect(chip?.className).toContain('bg-gain-soft')
    expect(chip?.className).toContain('text-green-600')
    expect(screen.getByText('vs 1M')).toBeTruthy()
  })

  it('renders an absent delta as a dash, never a zero', () => {
    render(<KpiCard label="Value" value="1" delta={{ pct: null }} />)
    expect(screen.getByText('—').parentElement?.className).toContain('bg-muted')
  })

  it('draws a sparkline under the value, and nothing for fewer than two points', () => {
    const { container, unmount } = render(<KpiCard label="x" value="1" sparkline={[1, 3, 2, 5]} />)
    expect(container.querySelectorAll('svg path').length).toBeGreaterThan(0)
    unmount()
    const lone = render(<KpiCard label="x" value="1" sparkline={[null, 4]} />)
    expect(lone.container.querySelector('svg')).toBeNull()
  })

  it('adds none of it when the props are absent', () => {
    const { container } = render(<KpiCard label="x" value="1" />)
    expect(container.querySelector('svg')).toBeNull()
    expect(container.querySelector('[aria-hidden="true"]')).toBeNull()
    expect(container.querySelector('.rounded-full')).toBeNull()
  })
})

/**
 * The backstop: a seventeenth card written by hand instead of through the primitive.
 *
 * This is the `noRawTables` pattern from `tableFamily.test.tsx`, for the same reason —
 * the cost of the duplication was not that any one copy was wrong, it was that changing
 * them all was sixteen mechanical edits, and nothing could see a new copy appearing.
 */
describe('no hand-rolled KPI cards', () => {
  /** The value class every one of the sixteen copies shared, verbatim. */
  const SIGNATURE = 'text-lg font-bold tabular-nums sm:text-2xl'

  it('nothing outside ui/ repeats the KPI value styling', () => {
    // `import.meta.glob` rather than node:fs, following tableFamily.test.tsx: the
    // frontend tsconfig is browser-targeted with no @types/node, so fs type-checks in
    // vitest and then fails `npm run build` — which is a worse failure than no test,
    // because it breaks the deploy rather than the suite.
    const sources = import.meta.glob('../*.tsx', {
      query: '?raw', import: 'default', eager: true,
    }) as Record<string, string>

    const offenders = Object.entries(sources)
      .filter(([path]) => !path.endsWith('.test.tsx'))
      .filter(([, text]) => text.includes(SIGNATURE))
      .map(([path]) => path.split('/').pop())

    expect(
      offenders,
      `${offenders.join(', ')} hand-rolls the KPI value class. Use <KpiCard> so the ` +
      `responsive size, the tone colours and the absent-means-a-dash rule stay in one place.`,
    ).toEqual([])
  })
})
