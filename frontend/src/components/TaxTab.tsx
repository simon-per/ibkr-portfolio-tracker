import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Card, CardContent } from '@/components/ui/card'
import {
  ArrowLeftRight, Building2, CalendarClock, CreditCard, Download, Globe, HandCoins, Hourglass,
  Landmark, Loader2, PiggyBank, ReceiptText, Scale, Telescope, TriangleAlert, Wallet,
} from 'lucide-react'
import { ABSENT, cn, formatPercent, formatSigned } from '@/lib/utils'
import { api } from '@/lib/api'
import { contributionsQuery, settingsQuery } from '@/lib/queryCache'
import { useFormatCurrency } from '@/lib/CurrencyContext'
import type { TaxReport } from '@/lib/api'
import { DataTable, type Column } from '@/components/ui/DataTable'
import { KpiCard, KpiPanel } from '@/components/ui/KpiCard'
import { SectionHeader } from '@/components/ui/SectionHeader'
import { SwissResidencyControl } from '@/components/SwissResidencyControl'
import { TickerAvatar } from '@/components/ui/TickerAvatar'
import type { Tone } from '@/lib/tone'
import { displayName } from '@/lib/securityName'
import {
  includedEstimatesTag, withheld, withholdingTotalLabel, type Money,
} from '@/lib/taxTotals'

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

/** The same three-way split as {@link gainClass}, for a KPI's tone. */
function gainTone(amount: number): Tone {
  return amount > 0 ? 'positive' : amount < 0 ? 'negative' : 'muted'
}

function signed(money: Money, amount: number): string {
  return formatSigned(amount, money)
}

/** A small rounded tag in a card header: `est.`, a source, an as-at date. */
const TAG = 'rounded-full px-2.5 py-1 text-xs font-medium'

/** The word on every totals row, quiet so the figures beside it carry the weight. */
const TOTAL_LABEL = (
  <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">Total</span>
)

/**
 * The symbol cell: logo (or monogram) plus ticker in the table, the ticker alone as the
 * phone card's title. The realized and holdings rows carry the security id as well as the
 * ISIN; a sale linked to no security has neither and keeps the monogram.
 */
function symbolCell(
  symbol: string | null,
  isin: string | null,
  view: 'table' | 'cards',
  securityId?: number | null,
) {
  if (!symbol) return ABSENT
  if (view === 'cards') return symbol
  return (
    <span className="flex items-center gap-2.5">
      {symbolAvatar(symbol, isin, 'h-7 w-7 text-[10px]', securityId)}
      <span>{symbol}</span>
    </span>
  )
}

/** The symbol cell's avatar, which the card title draws too (`DataTable`'s `avatar`). */
function symbolAvatar(
  symbol: string | null,
  isin: string | null,
  className: string,
  securityId?: number | null,
) {
  if (!symbol) return null
  return <TickerAvatar symbol={symbol} securityId={securityId ?? undefined} isin={isin} className={className} />
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
    avatar: (d, className) => symbolAvatar(d.symbol, d.isin, className),
    cell: (d, view) => symbolCell(d.symbol, d.isin, view),
  },
  {
    key: 'description',
    header: 'Description',
    shortHeader: 'Description',
    mobile: 'meta',
    cellClassName: 'max-w-[240px] truncate text-muted-foreground',
    cell: (d) => (d.description ? displayName(d.description) : ABSENT),
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
    cell: (d) => (d.withholding == null ? ABSENT : withheld(money, d.withholding)),
  },
]

/**
 * Each country row says how many of its payments were estimates, so only the countries
 * whose withholding is incomplete are tagged: absent when all of a country's payments
 * are estimates, the recorded sum with `excl. est.` when some are.
 */
const BY_COUNTRY_COLUMNS = (money: Money): Column<ByCountryRow>[] => [
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
      c.withholding == null ? (
        ABSENT
      ) : c.estimated_payments > 0 ? (
        <>
          {withheld(money, c.withholding)}
          <span className="ml-1 text-xs text-muted-foreground">excl. est.</span>
        </>
      ) : (
        withheld(money, c.withholding)
      ),
  },
  {
    key: 'rate_pct',
    header: 'Rate',
    shortHeader: 'Rate',
    align: 'right',
    cellClassName: 'text-muted-foreground',
    cell: (c) =>
      c.rate_pct == null ? (
        ABSENT
      ) : (
        <span title={c.residual_pct == null ? undefined : `Treaty ${c.residual_pct}%`}>
          {formatPercent(c.rate_pct)}
        </span>
      ),
  },
  {
    key: 'creditable',
    header: 'Creditable',
    shortHeader: 'Creditable',
    align: 'right',
    cell: (c) => (c.creditable == null ? ABSENT : money(c.creditable)),
  },
  {
    key: 'excess',
    header: 'Excess',
    shortHeader: 'Excess',
    align: 'right',
    tone: (c) => (c.excess ? 'text-warn' : undefined),
    cell: (c) => (c.excess == null ? ABSENT : money(c.excess)),
  },
]

const REALIZED_COLUMNS = (money: Money): Column<RealizedRow>[] => [
  {
    key: 'symbol',
    header: 'Symbol',
    shortHeader: 'Symbol',
    mobile: 'title',
    cellClassName: 'font-medium',
    avatar: (r, className) => symbolAvatar(r.symbol, r.isin, className, r.security_id),
    cell: (r, view) => symbolCell(r.symbol, r.isin, view, r.security_id),
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
    cell: (r) => (r.quantity != null ? r.quantity : ABSENT),
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
    avatar: (h, className) => symbolAvatar(h.symbol, h.isin, className, h.security_id),
    cell: (h, view) => symbolCell(h.symbol, h.isin, view, h.security_id),
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
    cell: (h) => (h.quantity != null ? h.quantity : ABSENT),
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
  const estimated = data.dividend_totals.estimated_rows
  const withholding = withholdingTotalLabel(money, data.dividend_totals.withholding, estimated)
  const included = includedEstimatesTag(estimated, rows.length)
  const withTag = (amount: string) => (
    <>
      {amount}
      {included && (
        <span className="ml-1 text-xs font-normal text-muted-foreground">{included}</span>
      )}
    </>
  )
  return (
    <DataTable
      rows={rows}
      columns={DIVIDEND_INCOME_COLUMNS(money)}
      getRowKey={(_, i) => i}
      label="Dividend income"
      density="compact"
      footer={[
        { key: 'label', spans: ['symbol', 'description', 'pay_date'], content: TOTAL_LABEL },
        { key: 'gross', spans: ['gross'], align: 'right', content: withTag(money(data.dividend_totals.gross)) },
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
        { key: 'net', spans: ['net'], align: 'right', content: withTag(money(data.dividend_totals.net)) },
      ]}
    />
  )
}

/**
 * The year in four figures, each one a totals row from a table below, printed with the same
 * honesty marks that row carries: `incl. N est.` / `excl. N est.` on the dividend figures,
 * `est.` on realized gains from closed lots, and a dash — never 0.00 — for a withholding
 * nothing recorded or a wealth-tax base that could not be built.
 */
function TaxSummary({ data, money }: { data: TaxReport; money: Money }) {
  const totals = data.dividend_totals
  const hasDividends = data.dividend_income.length > 0
  // Some rows estimated: the footer's own tag. All of them: the card's `est.` badge.
  const allEstimated = hasDividends && totals.estimated_rows >= data.dividend_income.length
  const estimateTag = allEstimated
    ? 'est.'
    : includedEstimatesTag(totals.estimated_rows, data.dividend_income.length)
  const withholding = withholdingTotalLabel(money, totals.withholding, totals.estimated_rows)
  const sales = data.realized_gains.length
  const gain = data.realized_totals.gain_loss
  const base = data.holdings_snapshot_error ? null : data.holdings_snapshot_total
  return (
    <KpiPanel columns={4}>
      <KpiCard
        tile
        icon={HandCoins}
        label="Dividends, net"
        value={money(totals.net)}
        sub={hasDividends
          ? [estimateTag, `gross ${money(totals.gross)}`].filter(Boolean).join(' · ')
          : undefined}
      />
      <KpiCard
        tile
        icon={Landmark}
        label="Withheld"
        value={totals.withholding == null ? null : withholding.amount}
        sub={withholding.excluded ?? undefined}
      />
      <KpiCard
        tile
        icon={ReceiptText}
        label="Realized gains"
        tone={sales > 0 ? gainTone(gain) : 'neutral'}
        iconTone={sales > 0 ? gainTone(gain) : 'neutral'}
        value={sales > 0 ? signed(money, gain) : money(0)}
        sub={sales > 0
          ? `${sales} ${sales === 1 ? 'sale' : 'sales'}${data.realized_source === 'trades' ? '' : ' · est.'}`
          : 'No sales'}
      />
      <KpiCard
        tile
        icon={Wallet}
        label="Steuerwert"
        value={base == null ? null : money(base)}
        sub={`as at ${data.holdings_as_of}`}
      />
    </KpiPanel>
  )
}

/**
 * The current year through 31 December: received plus the dividend forecast's expected, so
 * every tile is `est.`. Absent for a past year — the server sends no `projected` block.
 */
function YearEndProjection({ data, money }: { data: TaxReport; money: Money }) {
  const p = data.projected
  if (!p) return null
  return (
    <KpiPanel columns={3}>
      <KpiCard tile icon={Telescope} label="Dividends to 31 Dec" value={money(p.gross)} sub="gross · est." />
      <KpiCard tile icon={CalendarClock} label="Still expected" value={money(p.expected_gross)} sub="gross · est." />
      <KpiCard
        tile
        icon={Landmark}
        label="Withheld to 31 Dec"
        value={p.withholding == null ? null : withheld(money, p.withholding)}
        sub={`${withheld(money, p.expected_withholding)} expected · est.`}
      />
    </KpiPanel>
  )
}

/**
 * The ESTV KS 36 securities-dealer indicators as figures, with no threshold beside them. A
 * dash is "not on record". Turnover is a multiple, as the circular states its test.
 */
function TraderCriteria({ data, money }: { data: TaxReport; money: Money }) {
  const tc = data.trader_criteria
  if (!tc) return null
  const held = tc.sales != null && tc.short_held_sales != null
    ? `${tc.short_held_sales} of ${tc.sales}`
    : null
  return (
    <div>
      <SectionHeader variant="bare" icon={Scale} title="Securities dealer (KS 36)" className="mb-3" />
      <KpiPanel columns={3}>
        <KpiCard
          tile
          icon={ArrowLeftRight}
          label="Turnover"
          value={tc.turnover_multiple == null ? null : `${tc.turnover_multiple.toFixed(1)}×`}
          sub={tc.turnover_estimated ? 'est.' : undefined}
        />
        <KpiCard
          tile
          icon={Hourglass}
          label="Held < 6 months"
          value={held}
          sub={tc.sales_holding_unknown > 0 ? `${tc.sales_holding_unknown} unknown` : undefined}
        />
        <KpiCard
          tile
          icon={CreditCard}
          label="Margin used"
          tone={tc.margin_used ? 'warning' : 'neutral'}
          value={tc.margin_used == null ? null : tc.margin_used ? 'Yes' : 'No'}
          sub={tc.margin_used && tc.min_cash_balance != null
            ? `low ${money(tc.min_cash_balance)}`
            : undefined}
        />
      </KpiPanel>
    </div>
  )
}

export function TaxTab() {
  const money: Money = useFormatCurrency()
  const currentYear = new Date().getFullYear()
  const [year, setYear] = useState(currentYear)

  // Shares Dashboard's query key, so TanStack Query serves it from cache rather than
  // refetching. Failure is non-fatal: the picker falls back to the hardcoded floor.
  const { data: contributions } = useQuery(contributionsQuery)
  const { data: settings } = useQuery(settingsQuery)

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
        <SectionHeader
          icon={Landmark}
          title="Tax Report"
          aside={
            <>
              <label htmlFor="tax-year" className="text-sm text-muted-foreground">
                Year
              </label>
              <select
                id="tax-year"
                value={year}
                onChange={(e) => setYear(Number(e.target.value))}
                className="h-9 rounded-lg border border-input bg-card px-3 text-sm font-medium"
              >
                {years.map((y) => (
                  <option key={y} value={y}>
                    {y}
                  </option>
                ))}
              </select>
              <SwissResidencyControl from={settings?.swiss_tax_residency_from ?? null} />
              <a
                href={api.getTaxReportCsvUrl(year)}
                download={`tax_report_${year}.csv`}
                className={cn(
                  'inline-flex h-9 items-center justify-center gap-2 whitespace-nowrap rounded-lg border border-input bg-card px-3 text-sm font-medium ring-offset-background transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2'
                )}
              >
                <Download className="h-4 w-4" />
                Download CSV
              </a>
            </>
          }
        />
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
            <Card className="border-warn/40 bg-warn-soft">
              <CardContent className="flex gap-3 py-4 text-sm text-warn">
                <TriangleAlert aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0" />
                <div className="space-y-1.5">
                  {data.warnings.map((warning, i) => (
                    <p key={i}>{warning}</p>
                  ))}
                </div>
              </CardContent>
            </Card>
          )}

          {/* A year that ended before the move has no Swiss figures; the warning above
              says so and there is nothing to show beneath it. */}
          {data.residency.covered_from !== null && (
          <>
          <TaxSummary data={data} money={money} />
          <YearEndProjection data={data} money={money} />

          {/* --- Pillar 3a --- Leads: it is what is *absent* from every section below. */}
          {data.pillar3a && (
            <Card>
              <SectionHeader icon={PiggyBank} title="Pillar 3a" />
              <CardContent>
                <div className="grid gap-4 sm:grid-cols-2">
                  <div className="rounded-xl border border-border/70 bg-raised p-4">
                    <div className="text-2xl font-semibold tracking-tight">
                      {money(data.pillar3a.contributions)}
                    </div>
                    <div className="mt-1 text-sm text-muted-foreground">
                      Contributions in {data.year} · deductible
                    </div>
                  </div>
                  <div className="rounded-xl border border-border/70 bg-raised p-4">
                    <div className="text-2xl font-semibold tracking-tight text-muted-foreground">
                      {data.pillar3a.holdings_value == null ? ABSENT : money(data.pillar3a.holdings_value)}
                    </div>
                    <div className="mt-1 text-sm text-muted-foreground">
                      Assets held
                    </div>
                  </div>
                </div>
              </CardContent>
            </Card>
          )}

          {/* --- Dividend income + withholding (DA-1) --- */}
          <Card>
            <SectionHeader
              icon={HandCoins}
              title="Dividend income & withholding (DA-1)"
              aside={
                <>
                {/* Only the `est.` mark survives (owner, 2026-10-14): which feed a figure came
                    from is not the reader's business, whether it is an estimate is. And only
                    over figures — an empty year has nothing to be an estimate of. */}
                {data.dividend_source !== 'ibkr' && data.dividend_income.length > 0 && (
                  <span
                    className={cn(TAG, 'bg-muted text-muted-foreground')}
                    title={data.dividend_source === 'mixed' && data.dividend_ibkr_from
                      ? `Broker figures from ${data.dividend_ibkr_from}`
                      : undefined}
                  >
                    {data.dividend_source === 'mixed' ? 'Partly est.' : 'est.'}
                  </span>
                )}
                </>
              }
            />
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
              <SectionHeader icon={Globe} title="Withholding by country (DA-1)" />
              <CardContent>
                <DataTable
                  rows={data.dividend_by_country}
                  columns={BY_COUNTRY_COLUMNS(money)}
                  getRowKey={(c) => c.country}
                  label="Withholding by country"
                  density="compact"
                />
              </CardContent>
            </Card>
          )}

          {/* --- Realized capital gains --- */}
          <Card>
            <SectionHeader
              icon={ReceiptText}
              title="Realized capital gains"
              aside={
                <>
                {/* Where the figures came from, so only when there are figures: a year with
                    no sale reports the fallback source, and "Estimated" over "No realized
                    sales" reads as a caveat about nothing. */}
                {data.realized_gains.length > 0 && (
                  <span
                    className={cn(
                      TAG,
                      data.realized_source === 'trades' ? 'bg-info-soft text-info' : 'bg-warn-soft text-warn',
                    )}
                    title={
                      data.realized_source === 'trades'
                        ? 'IBKR trades, FIFO'
                        : 'Closed lots at close-date price'
                    }
                  >
                    {data.realized_source === 'trades' ? 'IBKR actual' : 'Estimated (closed lots)'}
                  </span>
                )}
                </>
              }
            />
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
                    { key: 'label', spans: ['symbol', 'trade_date'], content: TOTAL_LABEL },
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
            <SectionHeader
              icon={Building2}
              title="Holdings snapshot (wealth tax / Steuerwert)"
              aside={
                <span className={cn(TAG, 'bg-muted text-muted-foreground')} title="Steuerwert date">
                  as at {data.holdings_as_of}
                </span>
              }
            />
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
                    { key: 'label', spans: ['symbol'], content: TOTAL_LABEL },
                    {
                      key: 'market_value',
                      spans: ['market_value'],
                      align: 'right',
                      content:
                        data.holdings_snapshot_total != null
                          ? money(data.holdings_snapshot_total)
                          : ABSENT,
                    },
                  ]}
                />
              )}
              {/* Beside the holdings total, not inside it: absent when unknown. */}
              {data.cash_balance != null && (
                <div className="mt-3 flex items-center justify-between border-t border-border/70 px-2 pt-3 text-sm">
                  <span className="font-medium">Cash</span>
                  <span
                    className={cn(
                      'font-medium tabular-nums',
                      data.cash_balance < 0 && 'text-red-600 dark:text-red-400',
                    )}
                  >
                    {money(data.cash_balance)}
                  </span>
                </div>
              )}
            </CardContent>
          </Card>

          <TraderCriteria data={data} money={money} />
          </>
          )}
        </>
      ) : null}
    </div>
  )
}
