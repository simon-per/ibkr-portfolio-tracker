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
    for (const tab of DASHBOARD_TABS) expect(parseTabHash(`#${tab}`)).toEqual({ tab, company: null })
    // An SEC filer by its bare CIK (every link before 2026-11-17) or its key; another
    // regulator's company by its key.
    expect(parseTabHash('#company/320193')).toEqual({ tab: 'company', company: 'sec:320193' })
    expect(parseTabHash('#company/sec:320193')).toEqual({ tab: 'company', company: 'sec:320193' })
    expect(parseTabHash('#company/dart:00164779')).toEqual({ tab: 'company', company: 'dart:00164779' })
    expect(parseTabHash('#company/dart%3A00164779')).toEqual({ tab: 'company', company: 'dart:00164779' })
    expect(parseTabHash('#company/lei:969500A1YF1XUYYXS284')).toEqual(
      { tab: 'company', company: 'lei:969500A1YF1XUYYXS284' },
    )
  })

  it('falls back to Performance for nothing, an unknown tab or a hidden one', () => {
    for (const hash of ['', '#', '#analytics', '#Dividends', '#crypto', '#company%2F1']) {
      expect(parseTabHash(hash), hash).toEqual(DEFAULT_ROUTE)
    }
  })

  it('keeps the tab but drops a malformed company id', () => {
    for (const hash of [
      '#company/', '#company/abc', '#company/0', '#company/-4', '#company/1/2', '#company/12345678901',
      '#company/dart:1', '#company/lei:short', '#company/xyz:1', '#company/sec:0', '#company/%E0%A4%A',
    ]) {
      expect(parseTabHash(hash), hash).toEqual({ tab: 'company', company: null })
    }
    // A company id means something on the Company tab only.
    expect(parseTabHash('#dividends/320193')).toEqual({ tab: 'dividends', company: null })
  })
})

describe('routeHash', () => {
  it('leaves Performance bare and round-trips everything else', () => {
    expect(routeHash(DEFAULT_ROUTE)).toBe('')
    expect(routeHash({ tab: 'tax', company: null })).toBe('#tax')
    expect(routeHash({ tab: 'company', company: 'sec:1045810' })).toBe('#company/1045810')
    expect(routeHash({ tab: 'company', company: 'dart:00164779' })).toBe('#company/dart:00164779')
    for (const company of ['sec:1045810', 'dart:00164779', 'lei:969500A1YF1XUYYXS284']) {
      expect(parseTabHash(companyHref(company))).toEqual({ tab: 'company', company })
    }
    for (const tab of DASHBOARD_TABS) {
      expect(parseTabHash(routeHash({ tab, company: null }))).toEqual({ tab, company: null })
    }
  })
})

describe('the route in the URL', () => {
  it('replaces the hash without a history entry and tells every reader', () => {
    const { result } = renderHook(() => useTabRoute())
    const entries = window.history.length

    act(() => replaceRoute({ tab: 'dividends', company: null }))
    expect(window.location.hash).toBe('#dividends')
    expect(result.current).toEqual({ tab: 'dividends', company: null })

    act(() => replaceRoute(DEFAULT_ROUTE))
    expect(window.location.hash).toBe('')
    expect(window.location.href.endsWith('/')).toBe(true)
    expect(result.current).toEqual(DEFAULT_ROUTE)
    expect(window.history.length).toBe(entries)
  })

  it('follows a link, which is an ordinary hash navigation', () => {
    const { result } = renderHook(() => useTabRoute())
    act(() => {
      window.location.hash = companyHref('sec:320193')
      window.dispatchEvent(new HashChangeEvent('hashchange'))
    })
    expect(result.current).toEqual({ tab: 'company', company: 'sec:320193' })
  })
})
