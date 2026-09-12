// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { readStored, writeStored } from './storage'

/**
 * The guard, and the rule that there is exactly one of it.
 *
 * Four components read and wrote `localStorage` bare while three others wrapped it in a
 * try/catch of their own — so whether a private-mode browser took a tab down depended on
 * which tab had stored a preference. The behavioural half below pins the guard; the scan
 * pins that nothing goes around it, which is the test CLAUDE.md asks for: "is there a copy
 * at all", not "do the copies agree".
 */

beforeEach(() => localStorage.clear())
afterEach(() => vi.restoreAllMocks())

describe('readStored / writeStored', () => {
  it('round-trips a value', () => {
    writeStored('k', 'v')
    expect(readStored('k')).toBe('v')
  })

  it('returns null for a key never written', () => {
    expect(readStored('never')).toBeNull()
  })

  it('removes the key when written null, rather than storing the string "null"', () => {
    writeStored('k', 'v')
    writeStored('k', null)
    expect(readStored('k')).toBeNull()
    expect(localStorage.getItem('k')).toBeNull()
  })

  it('returns null instead of throwing when the store refuses access', () => {
    // Private-mode Safari and some embedded webviews throw on the accessor itself.
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('SecurityError')
    })
    expect(readStored('k')).toBeNull()
  })

  it('swallows a refused write', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceededError')
    })
    expect(() => writeStored('k', 'v')).not.toThrow()
  })
})

describe('every localStorage access goes through this module', () => {
  it('finds no bare localStorage access outside lib/storage.ts', () => {
    // `.test.` files are exempt: they seed and inspect the store directly, on purpose.
    const sources = import.meta.glob('../**/*.{ts,tsx}', {
      query: '?raw',
      import: 'default',
      eager: true,
    }) as Record<string, string>
    const offenders = Object.entries(sources)
      .filter(([path]) => !/(^|\/)storage\.ts$/.test(path) && !/\.test\.tsx?$/.test(path))
      .filter(([, source]) => /\blocalStorage\s*[.[]/.test(source))
      .map(([path]) => path)
    expect(offenders).toEqual([])
  })
})
