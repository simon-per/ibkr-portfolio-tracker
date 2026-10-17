// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { SECTION_OPEN_PREFIX, readStored, useRememberedOpen, writeStored } from './storage'

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

describe('useRememberedOpen', () => {
  it('starts at the section default and remembers a change for the next mount', () => {
    const first = renderHook(() => useRememberedOpen('demo'))
    expect(first.result.current[0]).toBe(false)
    act(() => first.result.current[1](true))
    expect(first.result.current[0]).toBe(true)
    first.unmount()

    const again = renderHook(() => useRememberedOpen('demo'))
    expect(again.result.current[0]).toBe(true)
  })

  it('reads anything but 1 or 0 as the default, never as an answer', () => {
    localStorage.setItem(`${SECTION_OPEN_PREFIX}demo`, 'yes')
    expect(renderHook(() => useRememberedOpen('demo', true)).result.current[0]).toBe(true)
    expect(renderHook(() => useRememberedOpen('demo', false)).result.current[0]).toBe(false)
  })

  it('takes a functional update, as a toggle passes one', () => {
    const { result } = renderHook(() => useRememberedOpen('demo', () => true))
    act(() => result.current[1]((open) => !open))
    expect(result.current[0]).toBe(false)
    expect(localStorage.getItem(`${SECTION_OPEN_PREFIX}demo`)).toBe('0')
  })
})
