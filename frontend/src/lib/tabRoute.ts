import { useMemo, useSyncExternalStore } from 'react'

/**
 * The open tab, and on the Company tab the open company, kept in the URL's hash.
 *
 * The tabs were uncontrolled state: a reload always landed on Performance, Back left the
 * app, and nothing could link to a tab or a company. Now `#dividends` is a tab and
 * `#company/<key>` a company, so a reload keeps both and a holding's row can link to its
 * company with a plain anchor.
 *
 * **One history entry per jump, none per tab click.** A tab click *replaces* the hash
 * (`replaceRoute`), so Back does not step through every tab you looked at; following a
 * company link is an ordinary anchor navigation, which *pushes* one, so Back returns to the
 * tab the click came from. `replaceState` fires no `hashchange`, hence `ROUTE_EVENT`: every
 * reader re-reads the hash after either kind of change.
 *
 * Stocks mode only. The crypto view never reads it, and switching back to stocks reads it
 * afresh. Performance is the default and keeps the URL clean (no hash).
 */

export const DASHBOARD_TABS = [
  'performance', 'dividends', 'lookthrough', 'allocation', 'activity',
  'fundamentals', 'company', 'watchlist', 'tax', 'forecast',
] as const

export type DashboardTab = (typeof DASHBOARD_TABS)[number]

export interface TabRoute {
  tab: DashboardTab
  /**
   * The company's key on `#company/<key>` (`sec:320193`, `dart:00164779`, `lei:<LEI>`); null
   * everywhere else. An SEC filer's hash is its bare CIK, as every link was before other
   * regulators' companies existed, so old URLs keep working.
   */
  company: string | null
}

export const DEFAULT_ROUTE: TabRoute = { tab: 'performance', company: null }

/** A company key as the server spells it, or a bare CIK. */
const COMPANY_REF = /^(?:(?:sec:)?[1-9]\d{0,9}|dart:\d{8}|lei:[0-9A-Z]{20})$/

const ROUTE_EVENT = 'portfolio:route'

export function isDashboardTab(value: string): value is DashboardTab {
  return (DASHBOARD_TABS as readonly string[]).includes(value)
}

/**
 * `#company/320193` → company `sec:320193`, `#company/dart:00164779` → that one; an unknown
 * tab, or a malformed one, → Performance.
 */
export function parseTabHash(hash: string): TabRoute {
  const [tab, id, ...rest] = hash.replace(/^#/, '').split('/')
  if (!isDashboardTab(tab)) return DEFAULT_ROUTE
  if (tab === 'company' && rest.length === 0 && id !== undefined) {
    let ref = id
    try {
      ref = decodeURIComponent(id)
    } catch {
      return { tab, company: null }
    }
    if (COMPANY_REF.test(ref)) return { tab, company: ref.includes(':') ? ref : `sec:${ref}` }
  }
  return { tab, company: null }
}

/** The hash for a route: empty for the default, so Performance's URL stays bare. */
export function routeHash({ tab, company }: TabRoute): string {
  if (tab === 'company' && company != null) return companyHref(company)
  return tab === DEFAULT_ROUTE.tab ? '' : `#${tab}`
}

/** An SEC filer by its bare CIK (the URLs that already exist), any other by its key. */
export function companyHref(key: string): string {
  return `#company/${key.startsWith('sec:') ? key.slice(4) : key}`
}

function subscribe(onChange: () => void): () => void {
  for (const type of ['hashchange', 'popstate', ROUTE_EVENT]) window.addEventListener(type, onChange)
  return () => {
    for (const type of ['hashchange', 'popstate', ROUTE_EVENT]) window.removeEventListener(type, onChange)
  }
}

const readHash = () => window.location.hash

/** The current route, re-read on every hash change (link, Back, `replaceRoute`). */
export function useTabRoute(): TabRoute {
  const hash = useSyncExternalStore(subscribe, readHash, () => '')
  return useMemo(() => parseTabHash(hash), [hash])
}

/** Move to `route` without a history entry: a tab click, or a pick in the Company picker. */
export function replaceRoute(route: TabRoute): void {
  const { pathname, search } = window.location
  const next = routeHash(route)
  if (next === window.location.hash) return
  window.history.replaceState(window.history.state, '', `${pathname}${search}${next}`)
  window.dispatchEvent(new Event(ROUTE_EVENT))
}
