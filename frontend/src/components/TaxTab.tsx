import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Download, Loader2 } from 'lucide-react'
import { cn } from '@/lib/utils'
import { api } from '@/lib/api'
import { useFormatCurrency } from '@/lib/CurrencyContext'
import type { TaxReport } from '@/lib/api'
import { DataTable, type Column } from '@/components/ui/DataTable'
import { displayName } from '@/lib/securityName'

/**
 * Only the first-paint fallback for the year picker. The real floor is the year of the
 * portfolio's first tax lot, read from /api/portfolio/contributions below — transferred
 * lots keep their **original** open_date, so a hardcoded year silently hides a whole
 * tax year once an older statement is ingested (which the planned 2025 backfill does).
 */
const FALLBACK_FIRST_TAX_YEAR = 2024

/** Green for gains, red for losses, muted for zero. */
function gainClass(amount: number): string {
  if (amount > 0) return 'text-green-600 dark:text-green-400'
  if (amount < 0) return 'text-red-600 dark:text-red-400'
  return 'text-muted-foreground'
}

type Money = (n: number) => string

/** Withholding is a deduction, so a recorded amount prints with its minus. */
function withheld(money: Money, amount: number): string {
  return amount ? `-${money(amount)}` : money(0)
}

function signed(money: Money, amount: number): string {
  return `${amount >= 0 ? '+' : ''}${money(amount)}`
}

/**
 * How many dividend rows are estimates. An estimate has a guessed gross and *no*
 * withholding on record — its 0 is "not known", not "nothing withheld" — so its
 * withholding cell is absent and a total built over it says how many it left out.
 */
export function estimatedDividendRows(rows: TaxReport['dividend_income']): number {
  return rows.filter((d) => d.source === 'yfinance_estimate').length
}

/**
 * The withholding total's cell. The backend's sum carries every estimate as 0, so it is
 * the recorded withholding only: absent when every row is an estimate, and tagged with
 * the count it excludes when some are.
 */
export function withholdingTotalLabel(
  money: Money,
  total: number,
  rowCount: number,
  estimatedRows: number,
): { amount: string; excluded: string | null } {
  if (rowCount > 0 && estimatedRows === rowCount) return { amount: '—', excluded: null }
  return {
    amount: withheld(money, total),
    excluded: estimatedRows > 0 ? `excl. ${estimatedRows} est.` : null,
  }
}
type DividendIncomeRow = TaxReport['dividend_income'][number]
type ByCountryRow = TaxReport['dividend_by_country'][number]
type RealizedRow = TaxReport['realized_gains'][number]
type HoldingRow = TaxReport['holdings_snapshot'][number]

/**
 * The four tax tables, described once each for both renderings.
 *
 * These are the ones that exercise DataTable's `footer`. `spans` names the columns a
 * total cell covers and the desktop `colSpan` is derived from it — better than the
 * literal `colSpan={3}` these carried, which lies silently the moment a column is
 * added, and which is exactly the class of quiet drift this file is otherwise careful
 * about. The fourth table's trailing empty `<td>` is derived too, from the spans not
 * covering the full width.
 */
const DIVIDEND_INCOME_COLUMNS = (money: Money): Column<DividendIncomeRow>[] => [
  {
    key: 'symbol',
    header: 'Symbol',
    shortHeader: 'Symbol',
    mobile: 'title',
    cellClassName: 'font-medium',
    cell: (d) => d.symbol ?? '—',
  },
  {
    key: 'description',
    header: 'Description',
    shortHeader: 'Description',
    mobile: 'meta',
    cellClassName: 'max-w-[240px] truncate text-muted-foreground',
    cell: (d) => (d.description ? displayName(d.description) : '—'),
  },
  {
    key: 'pay_date',
    header: 'Pay date',
    shortHeader: 'Pay date',
    mobile: 'meta',
    cell: (d) => d.pay_date,
  },
  {
    key: 'net',
    header: 'Net',
    shortHeader: 'Net',
    align: 'right',
    mobile: 'value',
    cellClassName: 'font-medium',
    cell: (d) => money(d.net),
  },
  {
    key: 'gross',
    header: 'Gross',
    shortHeader: 'Gross',
    align: 'right',
    cell: (d) => money(d.gross),
  },
  {
    key: 'withholding',
    header: 'Withholding',
    shortHeader: 'Withholding',
    align: 'right',
    cellClassName: 'text-muted-foreground',
    cell: (d) => (d.source === 'yfinance_estimate' ? '—' : withheld(money, d.withholding)),
  },
]

/**
 * A country row does not say which of its payments were estimates, so the column
 * follows the report's era: absent in an estimate-only year, tagged in the boundary
 * year, where its sum is the recorded withholding only.
 */
const BY_COUNTRY_COLUMNS = (
  money: Money,
  dividendSource: TaxReport['dividend_source'],
): Column<ByCountryRow>[] => [
  {
    key: 'country',
    header: 'Country',
    shortHeader: 'Country',
    mobile: 'title',
    cellClassName: 'font-medium',
    cell: (c) => c.country,
  },
  {
    key: 'net',
    header: 'Net',
    shortHeader: 'Net',
    align: 'right',
    mobile: 'value',
    cellClassName: 'font-medium',
    cell: (c) => money(c.net),
  },
  {
    key: 'positions',
    header: 'Positions',
    shortHeader: 'Positions',
    align: 'right',
    cellClassName: 'text-muted-foreground',
    cell: (c) => c.positions,
  },
  {
    key: 'gross',
    header: 'Gross',
    shortHeader: 'Gross',
    align: 'right',
    cell: (c) => money(c.gross),
  },
  {
    key: 'withholding',
    header: 'Withholding',
    shortHeader: 'Withholding',
    align: 'right',
    cell: (c) =>
      dividendSource === 'yfinance_estimate' ? (
        '—'
      ) : dividendSource === 'mixed' ? (
        <>
          {withheld(money, c.withholding)}
          <span className="ml-1 text-xs text-muted-foreground">excl. est.</span>
        </>
      ) : (
        withheld(money, c.withholding)
      ),
  },
]

const REALIZED_COLUMNS = (money: Money): Column<RealizedRow>[] => [
  {
    key: 'symbol',
    header: 'Symbol',
    shortHeader: 'Symbol',
    mobile: 'title',
    cellClassName: 'font-medium',
    cell: (r) => r.symbol ?? '—',
  },
  {
    key: 'trade_date',
    header: 'Trade date',
    shortHeader: 'Trade date',
    mobile: 'meta',
    cell: (r) => r.trade_date,
  },
  {
    key: 'gain_loss',
    header: 'Gain / Loss',
    shortHeader: 'Gain / Loss',
    align: 'right',
    // The point of the table, so it takes the card's headline slot rather than
    // proceeds — this is the figure a filing is checking.
    mobile: 'value',
    tone: (r) => gainClass(r.gain_loss),
    cellClassName: 'font-medium',
    cell: (r) => signed(money, r.gain_loss),
  },
  {
    key: 'quantity',
    header: 'Quantity',
    shortHeader: 'Quantity',
    align: 'right',
    cell: (r) => (r.quantity != null ? r.quantity : '—'),
  },
  {
    key: 'proceeds',
    header: 'Proceeds',
    shortHeader: 'Proceeds',
    align: 'right',
    cell: (r) => money(r.proceeds),
  },
  {
    key: 'cost_basis',
    header: 'Cost basis',
    shortHeader: 'Cost basis',
    align: 'right',
    cellClassName: 'text-muted-foreground',
    cell: (r) => money(r.cost_basis),
  },
]

const HOLDINGS_COLUMNS = (money: Money): Column<HoldingRow>[] => [
  {
    key: 'symbol',
    header: 'Symbol',
    shortHeader: 'Symbol',
    mobile: 'title',
    cellClassName: 'font-medium',
    cell: (h) => h.symbol ?? '—',
  },
  {
    key: 'market_value',
    header: 'Market value',
    shortHeader: 'Market value',
    align: 'right',
    mobile: 'value',
    cellClassName: 'font-medium',
    cell: (h) => money(h.market_value),
  },
  {
    key: 'quantity',
    header: 'Quantity',
    shortHeader: 'Quantity',
    align: 'right',
    cell: (h) => (h.quantity != null ? h.quantity : '—'),
  },
  {
    key: 'cost_basis',
    header: 'Cost basis',
    shortHeader: 'Cost basis',
    align: 'right',
    cellClassName: 'text-muted-foreground',
    cell: (h) => money(h.cost_basis),
  },
]

function DividendIncomeTable({ data, money }: { data: TaxReport; money: Money }) {
  const rows = data.dividend_income
  const withholding = withholdingTotalLabel(
    money,
    data.dividend_totals.withholding,
    rows.length,
    estimatedDividendRows(rows),
  )
  return (
    <DataTable
      rows={rows}
      columns={DIVIDEND_INCOME_COLUMNS(money)}
      getRowKey={(_, i) => i}
      label="Dividend income"
      density="compact"
      footer={[
        { key: 'label', spans: ['symbol', 'description', 'pay_date'], content: 'Total' },
        { key: 'gross', spans: ['gross'], align: 'right', content: money(data.dividend_totals.gross) },
        {
          key: 'withholding',
          spans: ['withholding'],
          align: 'right',
          content: (
            <>
              {withholding.amount}
              {withholding.excluded && (
                <span className="ml-1 text-xs font-normal text-muted-foreground">
                  {withholding.excluded}
                </span>
              )}
            </>
          ),
        },
        { key: 'net', spans: ['net'], align: 'right', content: money(data.dividend_totals.net) },
      ]}
    />
  )
}

export function TaxTab() {
  const money: Money = useFormatCurrency()
  const currentYear = new Date().getFullYear()
  const [year, setYear] = useState(currentYear)

  // Shares Dashboard's query key, so TanStack Query serves it from cache rather than
  // refetching. Failure is non-fatal: the picker falls back to the hardcoded floor.
  const { data: contributions } = useQuery({
    queryKey: ['portfolio', 'contributions'],
    queryFn: () => api.getContributions(),
    staleTime: 30 * 60 * 1000,
  })

  const inceptionYear = contributions?.first_contribution_date
    ? Number(contributions.first_contribution_date.slice(0, 4))
    : FALLBACK_FIRST_TAX_YEAR
  const firstTaxYear = Math.min(inceptionYear, FALLBACK_FIRST_TAX_YEAR)

  const years: number[] = []
  for (let y = currentYear; y >= firstTaxYear; y--) years.push(y)

  const { data, isLoading, isError, error } = useQuery<TaxReport>({
    queryKey: ['tax', 'report', year],
    queryFn: () => api.getTaxReport(year),
    staleTime: 30 * 60 * 1000, // 30 min
  })

  return (
    <div className="space-y-6">
      {/* Header: year selector + CSV download */}
      <Card>
        <CardHeader>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <CardTitle>Tax Report</CardTitle>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <label htmlFor="tax-year" className="text-sm text-muted-foreground">
                Year
              </label>
              <select
                id="tax-year"
                value={year}
                onChange={(e) => setYear(Number(e.target.value))}
                className="h-9 rounded-lg border border-input bg-background px-3 text-sm font-medium"
              >
                {years.map((y) => (
                  <option key={y} value={y}>
                    {y}
                  </option>
                ))}
              </select>
              <a
                href={api.getTaxReportCsvUrl(year)}
                download={`tax_report_${year}.csv`}
                className={cn(
                  'inline-flex h-9 items-center justify-center gap-2 whitespace-nowrap rounded-lg border border-input bg-background px-3 text-sm font-medium ring-offset-background transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2'
                )}
              >
                <Download className="h-4 w-4" />
                Download CSV
              </a>
            </div>
          </div>
        </CardHeader>
      </Card>

      {isLoading ? (
        <div className="flex items-center justify-center gap-2 py-16 text-muted-foreground">
          <Loader2 className="h-5 w-5 animate-spin" /> Loading tax report…
        </div>
      ) : isError ? (
        <Card>
          <CardContent className="py-8 text-center text-sm text-red-600 dark:text-red-400">
            Failed to load tax report: {error instanceof Error ? error.message : 'Unknown error'}
          </CardContent>
        </Card>
      ) : data ? (
        <>
          {/* A warning rides on a *successful* report — an omitted row or a
              missing wealth-tax base is invisible unless it is rendered. */}
          {data.warnings && data.warnings.length > 0 && (
            <Card className="border-amber-300 dark:border-amber-900/60">
              <CardContent className="space-y-1.5 py-4 text-sm text-amber-800 dark:text-amber-200">
                {data.warnings.map((warning, i) => (
                  <p key={i}>{warning}</p>
                ))}
              </CardContent>
            </Card>
          )}

          {/* --- Pillar 3a --- Leads: it is what is *absent* from every section below. */}
          {data.pillar3a && (
            <Card>
              <CardHeader>
                <CardTitle>Pillar 3a</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="grid gap-4 sm:grid-cols-2">
                  <div>
                    <div className="text-2xl font-semibold tabular-nums">
                      {money(data.pillar3a.contributions)}
                    </div>
                    <div className="text-sm text-muted-foreground">
                      Contributions in {data.year} · deductible
                    </div>
                  </div>
                  <div>
                    <div className="text-2xl font-semibold tabular-nums text-muted-foreground">
                      {money(data.pillar3a.holdings_value)}
                    </div>
                    <div className="text-sm text-muted-foreground">
                      Assets held
                    </div>
                  </div>
                </div>
              </CardContent>
            </Card>
          )}

          {/* --- Dividend income + withholding (DA-1) --- */}
          <Card>
            <CardHeader>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <CardTitle>Dividend income &amp; withholding (DA-1)</CardTitle>
                {/* Only the `est.` mark survives (owner, 2026-10-14): which feed a figure came
                    from is not the reader's business, whether it is an estimate is. */}
                {data.dividend_source !== 'ibkr' && (
                  <span
                    className="rounded-full bg-muted px-2.5 py-1 text-xs font-medium text-muted-foreground"
                    title={data.dividend_source === 'mixed' && data.dividend_ibkr_from
                      ? `Broker figures from ${data.dividend_ibkr_from}`
                      : undefined}
                  >
                    {data.dividend_source === 'mixed' ? 'Partly est.' : 'est.'}
                  </span>
                )}
              </div>
            </CardHeader>
            <CardContent>
              {data.dividend_income.length === 0 ? (
                <p className="py-4 text-center text-sm text-muted-foreground">
                  No dividend income recorded for {year}.
                </p>
              ) : (
                <DividendIncomeTable data={data} money={money} />
              )}
            </CardContent>
          </Card>

          {/* --- Withholding by country (DA-1 is filed per source country) --- */}
          {data.dividend_by_country.length > 0 && (
            <Card>
              <CardHeader>
                <CardTitle>Withholding by country (DA-1)</CardTitle>
              </CardHeader>
              <CardContent>
                <DataTable
                  rows={data.dividend_by_country}
                  columns={BY_COUNTRY_COLUMNS(money, data.dividend_source)}
                  getRowKey={(c) => c.country}
                  label="Withholding by country"
                  density="compact"
                />
              </CardContent>
            </Card>
          )}

          {/* --- Realized capital gains --- */}
          <Card>
            <CardHeader>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <CardTitle>Realized capital gains</CardTitle>
                <span
                  className={cn(
                    'rounded-full px-2.5 py-1 text-xs font-medium',
                    data.realized_source === 'trades'
                      ? 'bg-teal-100 text-teal-800 dark:bg-teal-950/60 dark:text-teal-200'
                      : 'bg-amber-100 text-amber-800 dark:bg-amber-950/60 dark:text-amber-200'
                  )}
                  title={
                    data.realized_source === 'trades'
                      ? 'IBKR trades, FIFO'
                      : 'Closed lots at close-date price'
                  }
                >
                  {data.realized_source === 'trades' ? 'IBKR actual' : 'Estimated (closed lots)'}
                </span>
              </div>
            </CardHeader>
            <CardContent>
              {data.realized_gains.length === 0 ? (
                <p className="py-4 text-center text-sm text-muted-foreground">
                  No realized sales recorded for {year}.
                </p>
              ) : (
                <DataTable
                  rows={data.realized_gains}
                  columns={REALIZED_COLUMNS(money)}
                  getRowKey={(_, i) => i}
                  label="Realized capital gains"
                  density="compact"
                  footer={[
                    { key: 'label', spans: ['symbol', 'trade_date'], content: 'Total' },
                    { key: 'proceeds', spans: ['proceeds'], align: 'right', content: money(data.realized_totals.proceeds) },
                    { key: 'cost_basis', spans: ['cost_basis'], align: 'right', content: money(data.realized_totals.cost_basis) },
                    {
                      key: 'gain_loss',
                      spans: ['gain_loss'],
                      align: 'right',
                      tone: gainClass(data.realized_totals.gain_loss),
                      content: signed(money, data.realized_totals.gain_loss),
                    },
                  ]}
                />
              )}
            </CardContent>
          </Card>

          {/* --- Holdings snapshot (wealth tax) --- */}
          <Card>
            <CardHeader>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <CardTitle>Holdings snapshot (wealth tax / Steuerwert)</CardTitle>
                <span
                  className="rounded-full bg-muted px-2.5 py-1 text-xs font-medium text-muted-foreground"
                  title="Steuerwert date"
                >
                  as at {data.holdings_as_of}
                </span>
              </div>
            </CardHeader>
            <CardContent>
              {data.holdings_snapshot_error ? (
                // A failure and an empty portfolio are different answers, and the
                // wealth-tax base is the last figure that should read as 0.00
                // because something upstream raised.
                <p className="py-4 text-center text-sm text-red-600 dark:text-red-400">
                  The snapshot could not be built, so no wealth-tax base is reported for{' '}
                  {data.holdings_as_of}. This is not zero.
                </p>
              ) : data.holdings_snapshot.length === 0 ? (
                <p className="py-4 text-center text-sm text-muted-foreground">No holdings to show.</p>
              ) : (
                <DataTable
                  rows={data.holdings_snapshot}
                  columns={HOLDINGS_COLUMNS(money)}
                  getRowKey={(_, i) => i}
                  label="Holdings snapshot"
                  density="compact"
                  footer={[
                    { key: 'label', spans: ['symbol'], content: 'Total' },
                    {
                      key: 'market_value',
                      spans: ['market_value'],
                      align: 'right',
                      content:
                        data.holdings_snapshot_total != null
                          ? money(data.holdings_snapshot_total)
                          : '—',
                    },
                  ]}
                />
              )}
            </CardContent>
          </Card>
        </>
      ) : null}
    </div>
  )
}
