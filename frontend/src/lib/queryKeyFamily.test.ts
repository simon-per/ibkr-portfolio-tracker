import { describe, expect, it } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative, sep } from 'node:path'

/**
 * A query read from two places is defined once, in `lib/queryCache.ts` (#70). Written out at
 * each reader, `['portfolio', 'contributions']` had four copies and the two value-over-time
 * copies disagreed on `staleTime` (30 minutes against the 5-minute default) for one cache entry.
 *
 * The scan reads every query *definition* — a `queryKey: [...]` that is not the filter of an
 * `invalidateQueries`/`removeQueries`/… call — and compares their shapes: string elements as
 * written, every other element as a wildcard, so `['dividends', 'breakdown', year]` and
 * `['dividends', 'breakdown', new Date().getFullYear()]` are the same definition twice.
 */
const SRC = join(__dirname, '..')
const rel = (p: string) => relative(SRC, p).split(sep).join('/')

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) return sources(path)
    return /\.(ts|tsx)$/.test(name) && !/\.test\.(ts|tsx)$/.test(name) ? [path] : []
  })
}

function code(path: string): string {
  return readFileSync(path, 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/\/\/.*$/gm, '')
}

/** `['a', x, 'b']` → `['a',*,'b']`. */
function keyShape(literal: string): string {
  const elements = literal.slice(1, -1).split(',').map((e) => e.trim()).filter(Boolean)
  return `[${elements.map((e) => (/^(['"]).*\1$/.test(e) ? e.replace(/"/g, "'") : '*')).join(',')}]`
}

/** Each query definition in a source: its key's shape. Filters on a client call are skipped. */
function definitions(source: string): string[] {
  return [...source.matchAll(/(Queries\(\{\s*)?queryKey:\s*(\[[^\]]*\])/g)]
    .filter(([, filter]) => !filter)
    .map(([, , literal]) => keyShape(literal))
}

describe('query definitions', () => {
  it('reads a key shape and tells a definition from a filter', () => {
    expect(keyShape("['dividends', 'breakdown', year]")).toBe("['dividends','breakdown',*]")
    expect(keyShape("['dividends', 'breakdown', new Date().getFullYear()]")).toBe("['dividends','breakdown',*]")
    expect(
      definitions("useQuery({ queryKey: ['a', 'b'], queryFn })\nclient.invalidateQueries({ queryKey: ['a'] })"),
    ).toEqual(["['a','b']"])
  })

  it('defines each query key once across the app', () => {
    const sites = new Map<string, string[]>()
    for (const path of sources(SRC)) {
      for (const shape of definitions(code(path))) {
        sites.set(shape, [...(sites.get(shape) ?? []), rel(path)])
      }
    }
    // The scan must be finding the definitions at all, or it passes on nothing.
    expect(sites.size).toBeGreaterThan(20)
    const duplicated = [...sites].filter(([, where]) => where.length > 1)
    expect(Object.fromEntries(duplicated)).toEqual({})
  })
})
