import { describe, expect, it } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative, sep } from 'node:path'
import { compareNullsLast } from './nullsLast'

/**
 * The four sortable tables share one order (`nullsLast.ts`). The family tests are the
 * scans: no module rolls the missing-last comparison again, and `ABSENT` is defined once.
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

  it('defines ABSENT once', () => {
    const offenders = sources(SRC)
      .filter((p) => /export const ABSENT\s*=/.test(code(p)))
      .map(rel)
    expect(offenders).toEqual(['lib/utils.ts'])
  })
})
