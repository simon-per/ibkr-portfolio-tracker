import { describe, expect, it } from 'vitest'

/**
 * One way to link a holding to its company.
 *
 * Every table listing held securities draws its identity through `CompanyLink`, which
 * resolves the row through the one lookup (`lib/companyLinks.ts`). A table that built its
 * own `#company/…` href would need its own lookup — the four tables, four lookups this
 * replaced before they were written.
 */
const sources = import.meta.glob(['./*.tsx', '../lib/*.ts'], { query: '?raw', import: 'default', eager: true })

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
