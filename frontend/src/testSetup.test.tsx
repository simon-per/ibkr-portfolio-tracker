// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { getConfig } from '@testing-library/react'

describe('the test setup', () => {
  it('gives async queries more than the 1 s default (see testSetup.ts)', () => {
    expect(getConfig().asyncUtilTimeout).toBe(4000)
  })

  it('gives jsdom a ResizeObserver', () => {
    const observer = new ResizeObserver(() => {})
    expect(() => {
      observer.observe(document.body)
      observer.disconnect()
    }).not.toThrow()
  })

  it('is the only place a test file stubs ResizeObserver by default', () => {
    // A copy per file is how there came to be ~27 in two styles. A test that counts or
    // records observations stubs its own with `vi.stubGlobal` — named here.
    const OWN_STUB = new Set(['lib/renderIdentity.test.tsx'])
    const files = import.meta.glob('./**/*.test.{ts,tsx}', { query: '?raw', import: 'default', eager: true })
    const offenders = Object.entries(files)
      .filter(([path]) => !OWN_STUB.has(path.replace(/^\.\//, '')) && path !== './testSetup.test.tsx')
      .filter(([, source]) =>
        /globalThis\.ResizeObserver\s*=|stubGlobal\(\s*'ResizeObserver'/.test(source as string))
      .map(([path]) => path)
    expect(offenders).toEqual([])
  })
})
