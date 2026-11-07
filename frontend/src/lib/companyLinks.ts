import { createContext, useContext } from 'react'
import type { CompanyFinancialsCompany } from './api'

/**
 * Which row of which table opens which company on the Company tab — the one lookup every
 * table of securities links through (`components/CompanyLink.tsx`).
 *
 * Built once from `/api/company-financials/companies`, the list the Company tab's own
 * picker reads, so a row can only link to a company the tab can show. A held row resolves
 * by its security (every held listing: `security_ids`, so GOOGL finds Alphabet as GOOG
 * does); a row that may be no security of ours — a watchlist ticker, a look-through
 * constituent, a tax row — by its ISIN against the company's, then by its ticker against
 * the company's symbol. Nothing found is no link, never a guess.
 *
 * The default context is the empty lookup, so a table rendered without the Dashboard's
 * provider — every component test — draws exactly what it drew before links existed.
 */

export interface CompanyLinks {
  bySecurity: ReadonlyMap<number, number>
  byIsin: ReadonlyMap<string, number>
  byTicker: ReadonlyMap<string, number>
}

export const NO_COMPANY_LINKS: CompanyLinks = { bySecurity: new Map(), byIsin: new Map(), byTicker: new Map() }

export function companyLinks(companies: readonly CompanyFinancialsCompany[]): CompanyLinks {
  const bySecurity = new Map<number, number>()
  const byIsin = new Map<string, number>()
  const byTicker = new Map<string, number>()
  for (const company of companies) {
    for (const id of company.security_ids ?? []) bySecurity.set(id, company.cik)
    if (company.security_id != null && !bySecurity.has(company.security_id)) {
      bySecurity.set(company.security_id, company.cik)
    }
    const isin = company.isin?.trim().toUpperCase()
    if (isin && !byIsin.has(isin)) byIsin.set(isin, company.cik)
    const ticker = company.symbol.trim().toUpperCase()
    if (ticker && !byTicker.has(ticker)) byTicker.set(ticker, company.cik)
  }
  return { bySecurity, byIsin, byTicker }
}

export interface CompanyRef {
  securityId?: number | null
  /** One ISIN, or several (a look-through constituent's listings). */
  isin?: string | null
  isins?: readonly string[] | null
  ticker?: string | null
}

/** The company's CIK, or null: by the security first, then by ISIN, then by the ticker. */
export function companyCikFor(links: CompanyLinks, { securityId, isin, isins, ticker }: CompanyRef): number | null {
  if (securityId != null) {
    const cik = links.bySecurity.get(securityId)
    if (cik != null) return cik
  }
  for (const code of [isin, ...(isins ?? [])]) {
    const cik = code ? links.byIsin.get(code.trim().toUpperCase()) : undefined
    if (cik != null) return cik
  }
  if (ticker) return links.byTicker.get(ticker.trim().toUpperCase()) ?? null
  return null
}

export const CompanyLinksContext = createContext<CompanyLinks>(NO_COMPANY_LINKS)

export function useCompanyCik(ref: CompanyRef): number | null {
  return companyCikFor(useContext(CompanyLinksContext), ref)
}
