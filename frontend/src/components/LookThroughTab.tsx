import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { SegmentedControl } from '@/components/ui/SegmentedControl'
import { api } from '@/lib/api'
import type { LookthroughCompanyRow, LookthroughFundCoverage } from '@/lib/api'
import { displayName } from '@/lib/securityName'
import { useFormatCurrency } from '@/lib/CurrencyContext'
import { KpiCard, KpiCardSkeleton } from '@/components/ui/KpiCard'
import { CollapsibleCardHeader } from '@/components/ui/CollapsibleCardHeader'
import { DataTable, type Column } from '@/components/ui/DataTable'
import { CompositionBar, ExposureTreemap } from './LookThroughCharts'
import { buildExposureGroups, buildPartition } from '@/lib/lookthroughChart'
import { cn, formatCount } from '@/lib/utils'

/**
 * True company-level exposure: direct holdings plus every held fund decomposed into the
 * companies inside it, folded across listings and share classes.
 *
 * **The coverage qualifier sits on the KPI row, outside every collapsible; the itemised
 * notes sit inside the collapsed Fund coverage card at the bottom.** Both halves are
 * deliberate. With some funds publishing no machine-readable basket, each company figure is
 * an understatement by whatever those funds hold — nothing is renormalised to hide that — so
 * the Coverage card's tone and footnote have to say so where people actually read, which is
 * the lesson `MonthlyReturnsHeatmap` learned when its `†` and its footnote both lived inside
 * a collapsed body while the summary carried the figure with no marker at all.
 *
 * The itemised `warnings[]` used to be a `role="alert"` block directly under that row, and by
 * 2026-09-08 it carried ten sentences — seven of them the same "basket is N days old" for
 * different funds — and pushed the company table, the thing the tab exists for, below the
 * fold. A banner that is always present and always long is the always-present-Flex-banner
 * pathology from the other direction: it teaches the reader to scroll past the surface that
 * also carries the one line that matters. The owner asked for it collapsed and moved down.
 * What has to stay unmissable is the qualifier, and the Coverage footnote now names every
 * condition present (no basket, borrowed basket, ageing) rather than only the most severe;
 * the collapsed card's own summary line counts the notes so their existence is never hidden,
 * only their text.
 *
 * **One thing is not a note and does not go in the collapsed card**: a holding the backend
 * could not value at all (`unvaluable_positions`). Its value is in neither the company
 * table nor the coverage denominator, so every percentage on the page is a share of a
 * smaller book than the one held — the same absence `AllocationTab` and the summary cards
 * state as a `role="alert"` above their figures. It rides the KPI row as one, naming the
 * symbols, because it was rendered nowhere until 2026-09-12 while the backend's schema
 * docstring asserted the frontend showed it.
 */

const LIMITS = [25, 50, 100] as const
type LimitOption = (typeof LIMITS)[number]

/**
 * The share of the book, in percent, at which identity-unresolved value turns the Unresolved
 * card amber. A handful of ISINs no provider resolves is the normal state (about 0.3% of the
 * book), and a card amber at any value above zero was amber always — the always-present
 * warning that teaches the reader to skip it. Below this it is a neutral figure.
 */
export const UNRESOLVED_WARN_PCT = 1

export function unresolvedTone(unresolvedValue: number, totalValue: number): 'warning' | 'neutral' {
  if (!(unresolvedValue > 0) || !(totalValue > 0)) return 'neutral'
  return (unresolvedValue / totalValue) * 100 >= UNRESOLVED_WARN_PCT ? 'warning' : 'neutral'
}

/** How a fund's status reads, and whether it counts against coverage. */
const FUND_STATUS_LABEL: Record<LookthroughFundCoverage['status'], string> = {
  looked_through: 'Covered',
  no_basket: 'No data',
  excluded: 'Excluded',
  implausible: 'Unusable data',
}

/** The company columns, described once for both renderings. */
export function lookthroughColumns(deps: {
  formatCurrency: (v: number) => string
}): Column<LookthroughCompanyRow>[] {
  const { formatCurrency } = deps
  return [
    {
      key: 'name',
      header: 'Company',
      shortHeader: 'Company',
      mobile: 'title',
      cellClassName: 'font-medium max-w-[260px] truncate',
      cell: (row) => displayName(row.name),
    },
    {
      key: 'listings',
      header: 'Held as',
      shortHeader: 'Held as',
      mobile: 'meta',
      cellClassName: 'text-muted-foreground text-xs max-w-[200px] truncate',
      hint: { description: 'Directly-held listings' },
      // Tickers only: the listing keys carry their exchange ("GOOG@NASDAQ") for identity.
      cell: (row) =>
        row.listings.length
          ? [...new Set(row.listings.map((l) => l.split('@')[0]))].join(', ')
          : null,
    },
    {
      key: 'value',
      header: 'Exposure',
      shortHeader: 'Exposure',
      align: 'right',
      mobile: 'value',
      cell: (row) => formatCurrency(row.value_eur),
    },
    {
      key: 'pct',
      header: '% of portfolio',
      shortHeader: '% of portfolio',
      align: 'right',
      mobile: 'delta',
      hint: { description: 'Share of the whole portfolio' },
      cell: (row) => `${row.pct_of_portfolio.toFixed(2)}%`,
    },
    {
      key: 'direct',
      header: 'Direct',
      shortHeader: 'Direct',
      align: 'right',
      hint: { description: 'Held directly' },
      cell: (row) => (row.direct_value_eur > 0 ? formatCurrency(row.direct_value_eur) : '—'),
    },
    {
      key: 'via_funds',
      header: 'Via funds',
      shortHeader: 'Via funds',
      align: 'right',
      hint: { description: 'Held through funds' },
      cell: (row) =>
        row.via_funds_value_eur > 0 ? formatCurrency(row.via_funds_value_eur) : '—',
    },
    {
      key: 'funds',
      header: 'Funds',
      shortHeader: 'Funds',
      align: 'right',
      hint: { description: 'Funds holding it' },
      cell: (row) => (row.via_funds.length ? String(row.via_funds.length) : '—'),
    },
  ]
}

/** The per-fund coverage columns — the honest half of the page. */
function fundColumns(deps: {
  formatCurrency: (v: number) => string
}): Column<LookthroughFundCoverage>[] {
  const { formatCurrency } = deps
  return [
    {
      key: 'symbol',
      header: 'Fund',
      shortHeader: 'Fund',
      mobile: 'title',
      cellClassName: 'font-medium',
      cell: (row) => row.symbol ?? row.fund_isin,
    },
    {
      key: 'status',
      header: 'Status',
      shortHeader: 'Status',
      mobile: 'badge',
      // A proxied fund is `looked_through` — its value really is attributed — but a green
      // "Decomposed" would claim its companies came from its own issuer, which is the one
      // thing this badge exists to say. Amber, naming the fund it borrowed from.
      cell: (row) => (
        <span
          className={
            row.status === 'looked_through' && !row.proxy_for_symbol
              ? 'text-xs rounded bg-emerald-100 px-1.5 py-0.5 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300'
              : 'text-xs rounded bg-amber-100 px-1.5 py-0.5 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300'
          }
        >
          {row.proxy_for_symbol
            ? `Via ${row.proxy_for_symbol}`
            : FUND_STATUS_LABEL[row.status]}
        </span>
      ),
    },
    {
      key: 'value',
      header: 'Value',
      shortHeader: 'Value',
      align: 'right',
      mobile: 'value',
      cell: (row) => formatCurrency(row.market_value_eur),
    },
    {
      key: 'as_of',
      header: 'Holdings as of',
      shortHeader: 'Holdings as of',
      align: 'right',
      hint: { description: "Issuer's basket date" },
      cell: (row) =>
        row.basket_as_of ? (
          <span className={row.stale ? 'text-amber-600 dark:text-amber-400' : undefined}>
            {row.basket_as_of}
          </span>
        ) : (
          '—'
        ),
    },
    {
      key: 'constituents',
      header: 'Holdings',
      shortHeader: 'Holdings',
      align: 'right',
      cell: (row) => (row.constituents ? String(row.constituents) : '—'),
    },
    {
      key: 'equity',
      header: 'Equity weight',
      shortHeader: 'Equity weight',
      align: 'right',
      hint: { description: 'Basket weight in securities' },
      cell: (row) =>
        row.equity_weight_pct === null ? '—' : `${row.equity_weight_pct.toFixed(2)}%`,
    },
  ]
}

/**
 * One company's breakdown, rendered INSIDE its own table row.
 *
 * It used to be a standalone `Card` with a title and a Close button, rendered after the
 * whole table — so pressing Breakdown on row 3 of 50 put the answer below row 50 and the
 * footnotes, one to three screens away, and the button looked inert. `DataTable`'s
 * `expandedRow` slot is what made a row-level home for it possible.
 *
 * The Card chrome went with the move rather than being kept: the row above it already
 * names the company and carries its value and percentage, and the button that opened it
 * already says **Hide**. A header repeating all three plus a second dismiss control is
 * noise once the panel is adjacent to its row.
 */
function CompanyBreakdown({
  row,
  formatCurrency,
}: {
  row: LookthroughCompanyRow
  formatCurrency: (v: number) => string
}) {
  return (
    <div className="space-y-3 text-sm">
      {row.listings.length > 0 && (
        <p>
          <span className="text-muted-foreground">Held directly as </span>
          {row.listings.join(', ')}
          <span className="text-muted-foreground">
            {' '}
            — {formatCurrency(row.direct_value_eur)}
          </span>
        </p>
      )}
      {row.via_funds.length > 0 && (
        <div>
          <p className="text-muted-foreground">Held through funds:</p>
          <ul className="mt-1 space-y-1">
            {row.via_funds.map((fund) => (
              <li key={fund.fund_isin} className="flex justify-between gap-4">
                <span>{fund.symbol ?? fund.fund_isin}</span>
                <span className="text-muted-foreground">
                  {fund.weight_pct.toFixed(2)}% of the fund ·{' '}
                  {formatCurrency(fund.value_eur)}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
      <p className="text-xs text-muted-foreground">
        {row.isins.length > 1
          ? `${row.isins.length} ISINs folded into this company: ${row.isins.join(', ')}`
          : `ISIN ${row.isins[0] ?? '—'}`}
      </p>
      {row.identity_conflicts.map((conflict) => (
        <p key={conflict} className="text-xs text-amber-700 dark:text-amber-400">
          {conflict}
        </p>
      ))}
    </div>
  )
}

export function LookThroughTab() {
  const [limit, setLimit] = useState<LimitOption>(50)
  const [selected, setSelected] = useState<string | null>(null)
  // Collapsed by default — see the module docstring for why the notes live in here.
  const [coverageOpen, setCoverageOpen] = useState(false)
  const formatCurrency = useFormatCurrency()

  const { data, isLoading, isError } = useQuery({
    queryKey: ['portfolio', 'lookthrough', limit],
    queryFn: () => api.getLookthrough(limit),
    staleTime: 30 * 60 * 1000,
  })

  const companyColumns = useMemo(
    () => lookthroughColumns({ formatCurrency }),
    [formatCurrency],
  )
  const coverageColumns = useMemo(() => fundColumns({ formatCurrency }), [formatCurrency])

  // The treemap and the table's Breakdown button share `selected`, so a tile click now
  // expands a row that may be far below the fold — the tile would look inert, which is the
  // very bug this change fixes arriving by the other route. So bring the panel into view.
  //
  // Two details are load-bearing. `inline: 'nearest'` because the row lives inside
  // `ScrollableTable`'s horizontal scroll container and the default would yank the table
  // sideways. And the OPTIONAL call, because jsdom implements no `scrollIntoView` at all —
  // the same gap that makes `useMediaQuery` fall back to desktop — so an unguarded call
  // throws in every test that renders this tab.
  const breakdownRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    if (!selected) return
    breakdownRef.current?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' })
  }, [selected])

  const sectorGroups = useMemo(() => (data ? buildExposureGroups(data) : []), [data])
  const partition = useMemo(() => (data ? buildPartition(data) : []), [data])

  // A fund contributes nothing to the company table while it reads either of these. `excluded`
  // is NOT one of them: DBPG is a settled decision, not an outstanding gap, so counting it here
  // would make the card permanently amber for a reason nobody can act on.
  const unresolvedFunds = (data?.funds ?? []).filter(
    (f) => f.status === 'no_basket' || f.status === 'implausible',
  )
  // A proxied fund IS attributed, so it never reaches the list above — but its companies come
  // from another fund's file, and a green "every fund decomposed" over that would be the
  // reassuring-zero failure this codebase keeps finding. It stays amber and, unlike a
  // percentage threshold, it can actually clear: importing a real basket for that fund wins
  // over the proxy with no code change.
  const proxiedFunds = (data?.funds ?? []).filter((f) => f.proxy_for_symbol)
  // Ageing baskets are the other way coverage lies: the percentage does not move, but the
  // holdings behind it describe an older index. Surfaced on the card, not only in the table.
  const staleFunds = (data?.funds ?? []).filter((f) => f.stale)

  // The Coverage footnote is now the ONLY qualifier above the fold, so it names every
  // condition present rather than the most severe one — the itemised list that used to sit
  // under it is inside the collapsed card at the bottom.
  const showUnidentified =
    !!data && unresolvedTone(data.identity.unresolved_value_eur, data.total_market_value_eur) === 'warning'
  const fundsWord = (n: number) => `${n} fund${n === 1 ? '' : 's'}`
  const coverageConditions = [
    unresolvedFunds.length > 0 ? `${fundsWord(unresolvedFunds.length)} without holdings` : null,
    proxiedFunds.length > 0 ? `${fundsWord(proxiedFunds.length)} via a similar fund` : null,
    staleFunds.length > 0 ? `${fundsWord(staleFunds.length)} with dated holdings` : null,
  ].filter((s): s is string => s !== null)
  const coverageSub = coverageConditions.length > 0 ? coverageConditions.join(' · ') : undefined

  // The collapsed card's one line. The backend's `warnings[]` stay in the API (and in
  // docs/lookthrough.md's reasoning); on screen the Coverage card names what is wrong.
  const funds = data?.funds ?? []
  const fundSummary = `${funds.length} fund${funds.length === 1 ? '' : 's'} held`

  // A fetch failure must not impersonate an empty portfolio — the table below would render
  // nothing and read as "you own no companies", which is the worst possible misreading of a
  // page whose entire purpose is completeness.
  if (isError) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Look-through exposure</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="py-8 text-center text-sm text-muted-foreground">
            Couldn't load look-through exposure — the server didn't respond. It retries
            automatically.
          </p>
        </CardContent>
      </Card>
    )
  }

  return (
    <div className="space-y-6">
      {isLoading || !data ? (
        <KpiCardSkeleton count={4} />
      ) : (
        <>
          {/* Above the KPI row and outside every collapsible — see the module docstring.
              Same shape and wording as AllocationTab's, since it is the same absence seen
              from the other tab. */}
          {data.unvaluable_positions > 0 && (
            <div
              role="alert"
              className="rounded-md border border-yellow-600/40 bg-yellow-600/10 px-3 py-2 text-xs text-yellow-700 dark:text-yellow-500"
            >
              <span className="font-medium">
                This view excludes {data.unvaluable_positions}{' '}
                {data.unvaluable_positions === 1 ? 'holding' : 'holdings'} with no usable
                price
                {data.unvaluable_symbols?.length
                  ? ` (${data.unvaluable_symbols.join(', ')})`
                  : ''}
              </span>
              {' '}— percentages are a share of what could be valued.
            </div>
          )}

          <div
            className={cn(
              'grid grid-cols-2 gap-3',
              showUnidentified ? 'sm:grid-cols-4' : 'sm:grid-cols-3',
            )}
          >
            <KpiCard
              label="Companies"
              value={data.company_count_total > 0 ? data.company_count_total.toLocaleString('en-US') : null}
            />
            {/*
              Toned on whether any fund is UNRESOLVED, not on a percentage threshold.
              `coverage_pct >= 95` was unreachable: DBPG is ~3.8% of the book and excluded by
              design, and no basket attributes 100% of its fund (part is always cash,
              derivatives and issuer rounding), so the practical ceiling is ~95.1-95.9% and the
              card would have sat amber forever with under a point of margin. A warning that can
              never clear is the always-present-Flex-banner pathology.

              "Every fund is either decomposed or deliberately excluded" IS reachable, and it
              correctly goes amber again the day a fund is bought that has no basket yet.
            */}
            <KpiCard
              label="Coverage"
              value={
                data.total_market_value_eur > 0 ? `${data.coverage_pct.toFixed(1)}%` : null
              }
              tone={
                unresolvedFunds.length === 0 && proxiedFunds.length === 0
                  ? 'positive'
                  : 'warning'
              }
              sub={coverageSub}
            />
            <KpiCard
              label="In funds"
              value={
                data.total_market_value_eur > 0
                  ? formatCurrency(
                      data.looked_through_equity_eur +
                        data.fund_residual_eur +
                        data.nested_fund_eur +
                        data.uncovered_fund_eur,
                    )
                  : null
              }
              sub={`${data.funds.length} funds held`}
            />
            {/* Only when it matters: a handful of unidentifiable securities is the normal
                state, and a card that always shows a count teaches the reader to skip it. */}
            {showUnidentified && (
              <KpiCard
                label="Unidentified"
                value={formatCurrency(data.identity.unresolved_value_eur)}
                tone="warning"
                sub={`${data.identity.unresolved_isins.toLocaleString('en-US')} securities`}
              />
            )}
          </div>

          {/*
            The itemised `warnings[]` used to render here as a `role="alert"` block. They are
            inside the collapsed Fund coverage card at the bottom now — see the module
            docstring. The Coverage card above carries the qualifier.
          */}

          {/*
            Its own card rather than a strip inside the one below, because the Top 25/50/100
            control belongs to the company table and this bar is not scoped by it — a
            portfolio-wide split sitting under a truncation control reads as though truncation
            moved it.
          */}
          {partition.length > 0 && (
            <Card>
              <CardHeader>
                <CardTitle>Where the value sits</CardTitle>
              </CardHeader>
              <CardContent>
                <CompositionBar segments={partition} formatCurrency={formatCurrency} />
              </CardContent>
            </Card>
          )}

          <Card>
            <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-3">
              <CardTitle>Company exposure</CardTitle>
              <SegmentedControl<LimitOption>
                label="How many companies to show"
                variant="buttons"
                value={limit}
                onChange={setLimit}
                options={LIMITS.map(option => ({ value: option, label: `Top ${option}` }))}
              />
            </CardHeader>
            <CardContent className="space-y-4">
              {data.companies.length === 0 ? (
                <p className="py-8 text-center text-sm text-muted-foreground">
                  No company exposure yet. Sync your IBKR data to see your holdings.
                </p>
              ) : (
                <>
                  <ExposureTreemap
                    groups={sectorGroups}
                    selected={selected}
                    onSelect={(key) => setSelected(key === selected ? null : key)}
                    formatCurrency={formatCurrency}
                  />
                  <DataTable
                    rows={data.companies}
                    columns={companyColumns}
                    getRowKey={(row) => row.company_key}
                    label="Company exposure"
                    detailLimit={4}
                    rowActions={(row) => (
                      <Button
                        variant="ghost"
                        size="sm"
                        aria-expanded={selected === row.company_key}
                        onClick={() =>
                          setSelected(
                            selected === row.company_key ? null : row.company_key,
                          )
                        }
                      >
                        {selected === row.company_key ? 'Hide' : 'Breakdown'}
                      </Button>
                    )}
                    // Beneath its own row, which is the whole point of this slot: the panel
                    // used to render after the entire card, so the answer to "what is inside
                    // Alphabet" appeared below the other 49 rows and the footnotes.
                    expandedRow={(row) =>
                      row.company_key === selected ? (
                        <div ref={breakdownRef}>
                          <CompanyBreakdown row={row} formatCurrency={formatCurrency} />
                        </div>
                      ) : null
                    }
                  />
                  {data.other_companies_count > 0 && (
                    <p className="text-xs text-muted-foreground">
                      +{formatCount(data.other_companies_count)} more ·{' '}
                      {formatCurrency(data.other_companies_eur)}
                    </p>
                  )}
                </>
              )}
            </CardContent>
          </Card>

          {/*
            Last on the page and collapsed by default, at the owner's request: the fund table,
            each fund's reason, and the itemised notes are reference material for the Coverage
            card above, not the content. The header's one line counts what is inside.
          */}
          {data.funds.length > 0 && (
            <Card>
              <CollapsibleCardHeader
                open={coverageOpen}
                onToggle={() => setCoverageOpen((o) => !o)}
                title="Fund coverage"
                description={fundSummary}
                contentId="lookthrough-fund-coverage"
              />
              {coverageOpen && (
                <CardContent id="lookthrough-fund-coverage" className="space-y-3">
                  <DataTable
                    rows={data.funds}
                    columns={coverageColumns}
                    getRowKey={(row) => row.fund_isin}
                    label="Fund coverage"
                    detailLimit={4}
                  />
                </CardContent>
              )}
            </Card>
          )}
        </>
      )}
    </div>
  )
}
