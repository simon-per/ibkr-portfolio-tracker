import { createContext, useContext } from 'react'
import type { CompanyFinancialsCompany } from './api'

/**
 * Which row of which table opens which company on the Company tab — the one lookup every
 * table of securities links through (`components/CompanyLink.tsx`).
 *
 * Built once from `/api/company-financials/companies`, the list the Company tab's own
 * picker reads, so a row can only link to a company the tab can show. A held row resolves
 * by its security (every held listing: `security_ids`, so GOOGL finds Alphabet as GOOG
 * does); a watchlist row, which may be no security of ours, also by its ticker against the
 * company's symbol. Nothing found is no link, never a guess.
 *
 * The default context is the empty lookup, so a table rendered without the Dashboard's
 * provider — every component test — draws exactly what it drew before links existed.
 */

export interface CompanyLinks {
  bySecurity: ReadonlyMap<number, number>
  byTicker: ReadonlyMap<string, number>
}

export const NO_COMPANY_LINKS: CompanyLinks = { bySecurity: new Map(), byTicker: new Map() }

export function companyLinks(companies: readonly CompanyFinancialsCompany[]): CompanyLinks {
  const bySecurity = new Map<number, number>()
  const byTicker = new Map<string, number>()
  for (const company of companies) {
    for (const id of company.security_ids ?? []) bySecurity.set(id, company.cik)
    if (company.security_id != null && !bySecurity.has(company.security_id)) {
      bySecurity.set(company.security_id, company.cik)
    }
    const ticker = company.symbol.trim().toUpperCase()
    if (ticker && !byTicker.has(ticker)) byTicker.set(ticker, company.cik)
  }
  return { bySecurity, byTicker }
}

export interface CompanyRef {
  securityId?: number | null
  ticker?: string | null
}

/** The company's CIK, or null: by the security first, then by the ticker. */
export function companyCikFor(links: CompanyLinks, { securityId, ticker }: CompanyRef): number | null {
  if (securityId != null) {
    const cik = links.bySecurity.get(securityId)
    if (cik != null) return cik
  }
  if (ticker) return links.byTicker.get(ticker.trim().toUpperCase()) ?? null
  return null
}

export const CompanyLinksContext = createContext<CompanyLinks>(NO_COMPANY_LINKS)

export function useCompanyCik(ref: CompanyRef): number | null {
  return companyCikFor(useContext(CompanyLinksContext), ref)
}
