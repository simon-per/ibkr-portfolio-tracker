import { useMemo, type MouseEvent, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { companyFinancialsCompaniesQuery } from '@/lib/queryCache'
import { CompanyLinksContext, NO_COMPANY_LINKS, companyLinks, useCompanyCik, type CompanyRef } from '@/lib/companyLinks'
import { companyHref } from '@/lib/tabRoute'
import { cn } from '@/lib/utils'

/**
 * The Dashboard's one reader of the company list for links. A row's link resolves through
 * the context it provides (`lib/companyLinks.ts`); outside it there are no links.
 */
export function CompanyLinksProvider({ children }: { children: ReactNode }) {
  const { data } = useQuery(companyFinancialsCompaniesQuery)
  const links = useMemo(() => (data ? companyLinks(data) : NO_COMPANY_LINKS), [data])
  return <CompanyLinksContext.Provider value={links}>{children}</CompanyLinksContext.Provider>
}

/**
 * A holding's identity — logo and name — as a link to its company on the Company tab, when
 * the tab has the company; otherwise its children exactly as they were. A plain anchor, so
 * the navigation pushes a history entry (Back returns to the row's tab) and a middle click
 * opens a new tab. No caption: the hover title is the whole explanation.
 */
export function CompanyLink({
  securityId,
  ticker,
  className,
  children,
}: CompanyRef & { className?: string; children: ReactNode }) {
  const cik = useCompanyCik({ securityId, ticker })
  if (cik == null) return <>{children}</>
  return (
    <a
      href={companyHref(cik)}
      title="Open company"
      onClick={toTop}
      className={cn(
        'rounded-sm transition-colors hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
        className,
      )}
    >
      {children}
    </a>
  )
}

/** The company opens at its top, not at the row's scroll depth. A modified click is the browser's. */
function toTop(e: MouseEvent<HTMLAnchorElement>) {
  if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
  window.requestAnimationFrame(() => window.scrollTo({ top: 0 }))
}
