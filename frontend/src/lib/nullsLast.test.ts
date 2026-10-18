import { describe, expect, it } from 'vitest'
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, relative, sep } from 'node:path'
import { compareNullsLast } from './nullsLast'

/**
 * The sortable tables share one order (`nullsLast.ts`): Fundamentals, the Watchlist, the
 * crypto holdings, the Dividends table and Positions. The family tests are the scans: no
 * module rolls the missing-last comparison or a direction-flipping comparator again, every
 * table that hands `DataTable` a sort reaches `compareNullsLast`, and `ABSENT` is defined
 * once.
 */
const SRC = join(__dirname, '..')

/** A source path relative to `src/`, with forward slashes on every OS. */
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
    .split('\n')
    .map((line) => line.replace(/\/\/.*$/, ''))
    .join('\n')
}

describe('compareNullsLast', () => {
  it('puts a missing value last in both directions, undefined included', () => {
    const values = [3, null, 1, undefined, 2]
    expect([...values].sort((a, b) => compareNullsLast(a, b, 'asc'))).toEqual([1, 2, 3, null, undefined])
    expect([...values].sort((a, b) => compareNullsLast(a, b, 'desc')).slice(0, 3)).toEqual([3, 2, 1])
    expect([...values].sort((a, b) => compareNullsLast(a, b, 'desc')).slice(3).every((v) => v == null)).toBe(true)
  })

  it('compares strings as a reader would, not by code unit', () => {
    const names = ['nestle', 'Nestlé', 'Apple']
    expect([...names].sort((a, b) => compareNullsLast(a, b, 'asc'))[0]).toBe('Apple')
    expect(compareNullsLast('a', 'B', 'asc')).toBeLessThan(0)
  })

  it('calls a tie a tie, for the caller to break', () => {
    expect(compareNullsLast(5, 5, 'desc')).toBe(0)
    expect(compareNullsLast(null, undefined, 'asc')).toBe(0)
  })
})

describe('the sort family', () => {
  it('has no hand-rolled missing-last comparison outside nullsLast.ts', () => {
    const pattern = /if \(\s*\w+\s*===?\s*null\s*\)\s*return\s+1\b/
    const offenders = sources(SRC)
      .filter((p) => !p.endsWith('nullsLast.ts'))
      .filter((p) => pattern.test(code(p)))
      .map(rel)
    expect(offenders).toEqual([])
  })

  // Positions passed the scan above with no null handling at all: its comparator was
  // `if (a < b) return dir === 'asc' ? -1 : 1`, which sorted a dash by its stand-in.
  it('has no hand-rolled direction-flipping comparator outside nullsLast.ts', () => {
    const pattern = /return\s+\w+\s*===?\s*'(asc|desc)'\s*\?\s*-?1\s*:\s*-?1\b/
    const offenders = sources(SRC)
      .filter((p) => !p.endsWith('nullsLast.ts'))
      .filter((p) => pattern.test(code(p)))
      .map(rel)
    expect(offenders).toEqual([])
  })

  it('sorts every DataTable through compareNullsLast, directly or via a module it imports', () => {
    const usesIt = (p: string) =>
      /import\s*\{[^}]*\bcompareNullsLast\b[^}]*\}\s*from\s*'[^']*nullsLast'/.test(code(p))
    /** Local modules a source imports, resolved to files (one hop: where comparators live). */
    const localImports = (p: string) =>
      [...code(p).matchAll(/from\s*'((?:\.{1,2}|@)\/[^']+)'/g)]
        .map(([, spec]) => (spec.startsWith('@/') ? join(SRC, spec.slice(2)) : join(dirname(p), spec)))
        .flatMap((base) => ['.ts', '.tsx'].map((ext) => base + ext))
        .filter(existsSync)
    const tables = sources(SRC)
      .filter((p) => !rel(p).startsWith('components/ui/'))
      .filter((p) => /ui\/DataTable'/.test(code(p)) && /\bsort=\{/.test(code(p)))
    expect(tables.length).toBeGreaterThanOrEqual(5)
    const offenders = tables
      .filter((p) => !usesIt(p) && !localImports(p).some(usesIt))
      .map(rel)
    expect(offenders).toEqual([])
  })

  it('defines ABSENT once', () => {
    const offenders = sources(SRC)
      .filter((p) => /export const ABSENT\s*=/.test(code(p)))
      .map(rel)
    expect(offenders).toEqual(['lib/utils.ts'])
  })
})
