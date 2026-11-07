// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import {
  DASHBOARD_TABS,
  DEFAULT_ROUTE,
  companyHref,
  parseTabHash,
  replaceRoute,
  routeHash,
  useTabRoute,
} from './tabRoute'

describe('parseTabHash', () => {
  it('reads every tab, and a company on the Company tab', () => {
    for (const tab of DASHBOARD_TABS) expect(parseTabHash(`#${tab}`)).toEqual({ tab, cik: null })
    expect(parseTabHash('#company/320193')).toEqual({ tab: 'company', cik: 320193 })
  })

  it('falls back to Performance for nothing, an unknown tab or a hidden one', () => {
    for (const hash of ['', '#', '#analytics', '#Dividends', '#crypto', '#company%2F1']) {
      expect(parseTabHash(hash), hash).toEqual(DEFAULT_ROUTE)
    }
  })

  it('keeps the tab but drops a malformed company id', () => {
    for (const hash of ['#company/', '#company/abc', '#company/0', '#company/-4', '#company/1/2', '#company/12345678901']) {
      expect(parseTabHash(hash), hash).toEqual({ tab: 'company', cik: null })
    }
    // A company id means something on the Company tab only.
    expect(parseTabHash('#dividends/320193')).toEqual({ tab: 'dividends', cik: null })
  })
})

describe('routeHash', () => {
  it('leaves Performance bare and round-trips everything else', () => {
    expect(routeHash(DEFAULT_ROUTE)).toBe('')
    expect(routeHash({ tab: 'tax', cik: null })).toBe('#tax')
    expect(routeHash({ tab: 'company', cik: 1045810 })).toBe(companyHref(1045810))
    for (const tab of DASHBOARD_TABS) {
      expect(parseTabHash(routeHash({ tab, cik: null }))).toEqual({ tab, cik: null })
    }
  })
})

describe('the route in the URL', () => {
  it('replaces the hash without a history entry and tells every reader', () => {
    const { result } = renderHook(() => useTabRoute())
    const entries = window.history.length

    act(() => replaceRoute({ tab: 'dividends', cik: null }))
    expect(window.location.hash).toBe('#dividends')
    expect(result.current).toEqual({ tab: 'dividends', cik: null })

    act(() => replaceRoute(DEFAULT_ROUTE))
    expect(window.location.hash).toBe('')
    expect(window.location.href.endsWith('/')).toBe(true)
    expect(result.current).toEqual(DEFAULT_ROUTE)
    expect(window.history.length).toBe(entries)
  })

  it('follows a link, which is an ordinary hash navigation', () => {
    const { result } = renderHook(() => useTabRoute())
    act(() => {
      window.location.hash = companyHref(320193)
      window.dispatchEvent(new HashChangeEvent('hashchange'))
    })
    expect(result.current).toEqual({ tab: 'company', cik: 320193 })
  })
})
