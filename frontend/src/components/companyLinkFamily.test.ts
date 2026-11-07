import { describe, expect, it } from 'vitest'

/**
 * One way to link a holding to its company.
 *
 * Every table listing held securities draws its identity through `CompanyLink`, which
 * resolves the row through the one lookup (`lib/companyLinks.ts`). A table that built its
 * own `#company/…` href would need its own lookup — the four tables, four lookups this
 * replaced before they were written.
 */
const sources = import.meta.glob(['./*.tsx', './ui/*.tsx', '../lib/*.ts'], { query: '?raw', import: 'default', eager: true })

/** Drawing one of these is naming a security or a company. */
const IDENTITY = /<(TickerAvatar|ConstituentAvatar|SymbolWithAvatar)\b/

/** The modules that draw an identity and rightly do not link it, each with its reason. */
const EXEMPT: Record<string, string> = {
  './CompanyTab.tsx': 'the Company page itself: its picker names the company it already shows',
  './LookThroughCharts.tsx':
    "the treemap's click selects the company in the table below, whose row links; its avatar sits in a tooltip",
  './ConstituentAvatar.tsx': 'the avatar itself',
  './ui/TickerAvatar.tsx': 'the avatar itself',
  './ui/DataTable.tsx': 'draws the avatar its columns hand it; the cell beside it carries the link',
}

const source = (path: string) => {
  const src = sources[path]
  expect(src, `${path} is missing`).toBeTypeOf('string')
  return src as string
}

describe('links to the Company tab', () => {
  it('every table of held securities links its identity through CompanyLink', () => {
    for (const path of ['./PositionsList.tsx', './DividendsTab.tsx', './FundamentalsTab.tsx', './watchlistColumns.tsx']) {
      // Both renderings of the identity: the desktop cell and the phone card's title.
      expect(source(path).match(/<CompanyLink\b/g)?.length ?? 0, path).toBeGreaterThanOrEqual(2)
    }
  })

  it('every component that names a security or a company links it (owner: "link everything")', () => {
    const unlinked = Object.entries(sources)
      .filter(([path]) => path.endsWith('.tsx') && !/\.test\.tsx$/.test(path))
      .filter(([path, src]) => IDENTITY.test(src as string) && !(path in EXEMPT))
      .filter(([, src]) => !/<CompanyLink\b/.test(src as string))
      .map(([path]) => path)
    expect(unlinked).toEqual([])
    // A stale exemption would wave through the next module of that name.
    for (const path of Object.keys(EXEMPT)) expect(sources[path], path).toBeTypeOf('string')
  })

  it('no module but the route builds a company href by hand', () => {
    // Comments may name the hash (`#company/<cik>`); code may not spell it.
    const code = (src: string) => src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/.*$/gm, '$1')
    const offenders = Object.entries(sources)
      .filter(([path]) => !/\.test\.tsx?$/.test(path) && path !== '../lib/tabRoute.ts')
      .filter(([, src]) => /['"`]#company\//.test(code(src as string)))
      .map(([path]) => path)
    expect(offenders).toEqual([])
    // The scan still sees a literal in code.
    expect(/['"`]#company\//.test(code("const a = `#company/${cik}` // see `#company/1`"))).toBe(true)
    expect(/['"`]#company\//.test(code('// a `#company/<cik>` hash\n/* `#company/1` */'))).toBe(false)
  })
})
