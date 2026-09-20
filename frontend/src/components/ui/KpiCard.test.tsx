// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { ABSENT, KpiCard, KpiCardSkeleton, KpiPanel } from './KpiCard'

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
    expect(screen.getByText('CHF 48,561').className).toContain('sm:text-2xl')
    unmount()

    render(<KpiCard hero label="Market Value" value="CHF 65,024" />)
    const hero = screen.getByText('CHF 65,024')
    expect(hero.className).toContain('text-2xl')
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
    expect(panel?.className).toContain('rounded-xl')
    expect(grid?.className).toContain('grid-cols-2')
    expect(grid?.children).toHaveLength(2)
    expect(grid?.children[0]?.className).toContain('p-4')
    expect(grid?.children[0]?.className).not.toContain('border')
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
