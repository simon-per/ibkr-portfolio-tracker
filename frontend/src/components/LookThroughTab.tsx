import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { api } from '@/lib/api'
import type { LookthroughCompanyRow, LookthroughFundCoverage } from '@/lib/api'
import { useFormatCurrency } from '@/lib/CurrencyContext'
import { KpiCard, KpiCardSkeleton } from '@/components/ui/KpiCard'
import { DataTable, type Column } from '@/components/ui/DataTable'

/**
 * True company-level exposure: direct holdings plus every held fund decomposed into the
 * companies inside it, folded across listings and share classes.
 *
 * **The coverage notice sits outside every collapsible, deliberately.** With some funds
 * publishing no machine-readable basket, each company figure is an understatement by
 * whatever those funds hold — and nothing is renormalised to hide that. A caveat reachable
 * only by expanding a card is absent from the surface people actually read, which is the
 * lesson `MonthlyReturnsHeatmap` learned when its `†` and its footnote both lived inside a
 * collapsed body while the summary carried the figure with no marker at all.
 */

const LIMITS = [25, 50, 100] as const
type LimitOption = (typeof LIMITS)[number]

/** How a fund's status reads, and whether it counts against coverage. */
const FUND_STATUS_LABEL: Record<LookthroughFundCoverage['status'], string> = {
  looked_through: 'Decomposed',
  no_basket: 'No basket',
  excluded: 'Excluded',
  implausible: 'Basket rejected',
}

/** What each identity tier means, shown as the row's badge title rather than a bare code. */
const KEY_TYPE_TITLE: Record<LookthroughCompanyRow['key_type'], string> = {
  lei: 'Folded on the issuer’s LEI, so different share classes are combined',
  share_class_figi: 'Folded on the share-class FIGI, so listings on several venues are combined',
  isin: 'Folded on the ISIN alone — no LEI or share-class FIGI is on record yet',
  unidentified: 'No identifier at all, so this row cannot be combined with anything',
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
      cell: (row) => row.name,
    },
    {
      key: 'listings',
      header: 'Held as',
      shortHeader: 'Held as',
      mobile: 'meta',
      cellClassName: 'text-muted-foreground text-xs max-w-[200px] truncate',
      hint: {
        description:
          'The directly-held listings folded into this row. Empty when the exposure comes only through funds.',
      },
      cell: (row) => (row.listings.length ? row.listings.join(', ') : null),
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
      hint: {
        description:
          'Share of the whole portfolio, including the funds that could not be decomposed. Never rescaled onto the covered part, so these figures are understatements while coverage is below 100%.',
      },
      cell: (row) => `${row.pct_of_portfolio.toFixed(2)}%`,
    },
    {
      key: 'direct',
      header: 'Direct',
      shortHeader: 'Direct',
      align: 'right',
      hint: { description: 'Held as a position in this company itself.' },
      cell: (row) => (row.direct_value_eur > 0 ? formatCurrency(row.direct_value_eur) : '—'),
    },
    {
      key: 'via_funds',
      header: 'Via funds',
      shortHeader: 'Via funds',
      align: 'right',
      hint: { description: 'Held indirectly, through the funds listed in the drill-down.' },
      cell: (row) =>
        row.via_funds_value_eur > 0 ? formatCurrency(row.via_funds_value_eur) : '—',
    },
    {
      key: 'funds',
      header: 'Funds',
      shortHeader: 'Funds',
      align: 'right',
      hint: { description: 'How many held funds contribute to this company.' },
      cell: (row) => (row.via_funds.length ? String(row.via_funds.length) : '—'),
    },
    {
      key: 'identity',
      header: 'Identity',
      shortHeader: 'Identity',
      align: 'right',
      mobile: 'badge',
      hint: {
        description:
          'Which identifier folded this row. A dagger means at least one holding carried only an ISIN, so a sibling listing under a different ISIN would not have joined.',
      },
      cell: (row) => (
        <span
          className="text-xs text-muted-foreground"
          title={KEY_TYPE_TITLE[row.key_type]}
        >
          {row.key_type === 'lei'
            ? 'LEI'
            : row.key_type === 'share_class_figi'
              ? 'FIGI'
              : row.key_type === 'isin'
                ? 'ISIN'
                : 'none'}
          {row.partially_resolved ? ' †' : ''}
        </span>
      ),
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
      cell: (row) => (
        <span
          className={
            row.status === 'looked_through'
              ? 'text-xs rounded bg-emerald-100 px-1.5 py-0.5 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300'
              : 'text-xs rounded bg-amber-100 px-1.5 py-0.5 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300'
          }
        >
          {FUND_STATUS_LABEL[row.status]}
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
      header: 'Basket as of',
      shortHeader: 'Basket as of',
      align: 'right',
      hint: {
        description:
          "The issuer's own as-of date where it publishes one. Vanguard publishes monthly and lags several weeks, which is normal rather than a fault.",
      },
      cell: (row) =>
        row.basket_as_of ? (
          <span className={row.stale ? 'text-amber-600 dark:text-amber-400' : undefined}>
            {row.basket_as_of}
            {row.stale ? ' †' : ''}
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
      hint: {
        description:
          "How much of the fund the stored basket attributes to securities. Genuinely below 100% for real funds: their files carry cash, FX and futures rows, and a broad fund also loses weight to rounding — Vanguard publishes weights to 2dp, so thousands of VT's smallest holdings round to zero.",
      },
      cell: (row) =>
        row.equity_weight_pct === null ? '—' : `${row.equity_weight_pct.toFixed(2)}%`,
    },
  ]
}

/**
 * Why each undecomposed fund is undecomposed, as prose.
 *
 * Deliberately NOT a column. `DataTable`'s phone card renders every detail into
 * `<dd class="shrink-0 tabular-nums">` — exactly right for a figure and exactly wrong for a
 * sentence, because `shrink-0` forces the element's intrinsic width. A `reason` column pushed
 * the page 1,282px sideways at 390px, which `e2e/mobile.mjs` caught and no unit test could:
 * jsdom loads no CSS, so overflow is invisible to it.
 *
 * Prose in prose also reads better on both viewports, and it must stay visible on the phone —
 * this is the explanation for a fifth of the portfolio being absent from the table above.
 */
function FundReasons({ funds }: { funds: LookthroughFundCoverage[] }) {
  const missing = funds.filter((f) => f.status !== 'looked_through' && f.reason)
  if (missing.length === 0) return null
  return (
    <dl className="space-y-1.5 text-xs">
      {missing.map((fund) => (
        <div key={fund.fund_isin} className="flex flex-col gap-0.5 sm:flex-row sm:gap-2">
          <dt className="font-medium sm:shrink-0">{fund.symbol ?? fund.fund_isin}</dt>
          <dd className="text-muted-foreground">{fund.reason}</dd>
        </div>
      ))}
    </dl>
  )
}

function CompanyDrillDown({
  row,
  onClose,
  formatCurrency,
}: {
  row: LookthroughCompanyRow
  onClose: () => void
  formatCurrency: (v: number) => string
}) {
  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-4">
        <div>
          <CardTitle className="text-sm">{row.name}</CardTitle>
          <CardDescription>
            {formatCurrency(row.value_eur)} · {row.pct_of_portfolio.toFixed(2)}% of the
            portfolio
          </CardDescription>
        </div>
        <Button variant="ghost" size="sm" onClick={onClose} aria-label="Close breakdown">
          Close
        </Button>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
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
      </CardContent>
    </Card>
  )
}

export function LookThroughTab() {
  const [limit, setLimit] = useState<LimitOption>(50)
  const [selected, setSelected] = useState<string | null>(null)
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
  const selectedRow = data?.companies.find((r) => r.company_key === selected) ?? null

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
            Couldn't load look-through exposure — the backend didn't respond. It retries
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
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <KpiCard
              label="Companies"
              value={data.company_count_total > 0 ? data.company_count_total : null}
              sub={`${data.companies_shown} shown`}
            />
            <KpiCard
              label="Coverage"
              value={
                data.total_market_value_eur > 0 ? `${data.coverage_pct.toFixed(1)}%` : null
              }
              tone={data.coverage_pct >= 95 ? 'positive' : 'warning'}
              sub="of the portfolio attributed to companies"
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
            <KpiCard
              label="Unresolved"
              value={data.identity.unresolved_isins}
              tone={data.identity.unresolved_value_eur > 0 ? 'warning' : 'neutral'}
              sub={
                data.identity.unresolved_value_eur > 0
                  ? `${formatCurrency(data.identity.unresolved_value_eur)} not folded by an identifier`
                  : 'every holding folded by an identifier'
              }
            />
          </div>

          {/* Outside every collapsible, on purpose — see the module docstring. */}
          {data.warnings.length > 0 && (
            <div
              role="alert"
              className="rounded-md border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200"
            >
              <p className="font-medium">This is a partial view</p>
              <ul className="mt-2 list-disc space-y-1 pl-5">
                {data.warnings.map((warning) => (
                  <li key={warning}>{warning}</li>
                ))}
              </ul>
            </div>
          )}

          <Card>
            <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-3">
              <div>
                <CardTitle>Company exposure</CardTitle>
                <CardDescription>
                  Direct holdings and fund constituents combined, with share classes and
                  multiple listings folded into one row. Percentages are of the whole
                  portfolio and are not rescaled.
                </CardDescription>
              </div>
              <div className="flex gap-1" role="group" aria-label="How many companies to show">
                {LIMITS.map((option) => (
                  <Button
                    key={option}
                    size="sm"
                    variant={option === limit ? 'default' : 'outline'}
                    aria-pressed={option === limit}
                    onClick={() => setLimit(option)}
                  >
                    Top {option}
                  </Button>
                ))}
              </div>
            </CardHeader>
            <CardContent className="space-y-4">
              {data.companies.length === 0 ? (
                <p className="py-8 text-center text-sm text-muted-foreground">
                  No company exposure yet. Sync your IBKR data to see your holdings.
                </p>
              ) : (
                <>
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
                        onClick={() =>
                          setSelected(
                            selected === row.company_key ? null : row.company_key,
                          )
                        }
                      >
                        {selected === row.company_key ? 'Hide' : 'Breakdown'}
                      </Button>
                    )}
                  />
                  {data.other_companies_count > 0 && (
                    <p className="text-xs text-muted-foreground">
                      A further {data.other_companies_count} companies hold{' '}
                      {formatCurrency(data.other_companies_eur)} between them and are not
                      shown.
                    </p>
                  )}
                  {data.companies.some((r) => r.partially_resolved) && (
                    <p className="text-xs text-muted-foreground">
                      † folded on an ISIN alone, so a listing under a different ISIN would
                      not have joined that row.
                    </p>
                  )}
                </>
              )}
            </CardContent>
          </Card>

          {selectedRow && (
            <CompanyDrillDown
              row={selectedRow}
              onClose={() => setSelected(null)}
              formatCurrency={formatCurrency}
            />
          )}

          {data.funds.length > 0 && (
            <Card>
              <CardHeader>
                <CardTitle>Fund coverage</CardTitle>
                <CardDescription>
                  Which held funds could be decomposed, and why the others could not. A fund
                  with no basket contributes nothing to the table above — its value is
                  counted in the portfolio but attributed to no company.
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-3">
                <DataTable
                  rows={data.funds}
                  columns={coverageColumns}
                  getRowKey={(row) => row.fund_isin}
                  label="Fund coverage"
                  detailLimit={4}
                />
                <FundReasons funds={data.funds} />
                <p className="text-xs text-muted-foreground">
                  Not attributed to any company:{' '}
                  {formatCurrency(
                    data.uncovered_fund_eur +
                      data.fund_residual_eur +
                      data.nested_fund_eur,
                  )}{' '}
                  — {formatCurrency(data.uncovered_fund_eur)} in funds with no usable
                  basket, and {formatCurrency(data.fund_residual_eur)} left over inside the
                  funds that were decomposed: cash, derivatives, and weight lost to rounding
                  in the issuer's own file
                  {data.nested_fund_eur > 0
                    ? `, plus ${formatCurrency(data.nested_fund_eur)} in funds held inside those funds`
                    : ''}
                  .
                </p>
                {data.oldest_basket_as_of && (
                  <p className="text-xs text-muted-foreground">
                    Oldest basket on record: {data.oldest_basket_as_of}.
                  </p>
                )}
              </CardContent>
            </Card>
          )}
        </>
      )}
    </div>
  )
}
