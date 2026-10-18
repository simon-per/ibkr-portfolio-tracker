import { TickerAvatar } from '@/components/ui/TickerAvatar'
import { useMemo, useState } from 'react'
import { hashKey, useQuery } from '@tanstack/react-query'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { SegmentedControl } from '@/components/ui/SegmentedControl'
import { SectionHeader } from '@/components/ui/SectionHeader'
import { KpiCard, KpiPanel } from '@/components/ui/KpiCard'
import { DataTable, type Column } from '@/components/ui/DataTable'
import {
  ArrowDownToLine, ArrowLeftRight, ArrowRightLeft, ArrowUpFromLine, Banknote, Download,
  CircleDollarSign, HandCoins, Hash, Minus, Plus, ShoppingCart, Split, Tag, Wallet, type LucideIcon,
} from 'lucide-react'
import { ABSENT, cn, formatCount, formatCurrency, formatDate, formatSigned, placeholderDim } from '@/lib/utils'
import { api } from '@/lib/api'
import { activityQuery, contributionsQuery } from '@/lib/queryCache'
import { useBaseCurrency, useKeepPreviousData } from '@/lib/CurrencyContext'
import { rangeFor, type TimeRange } from '@/lib/dateRanges'
import { isSell, summarizeActivity, type ActivitySummary } from '@/lib/activitySummary'
import { TONE_CHIP, TONE_TEXT, type Tone } from '@/lib/tone'
import type { ActivityKind, ActivityParams, ActivityRow } from '@/lib/api'

const PAGE_SIZE = 100

/** Two ledger requests that differ in nothing but the page. */
function isPageTurn(previous: unknown, next: ActivityParams): boolean {
  if (typeof previous !== 'object' || previous === null) return false
  return hashKey([{ ...previous, offset: 0 }]) === hashKey([{ ...next, offset: 0 }])
}

/**
 * The figures strip needs every row of the window, so a window longer than one page is
 * fetched once more, whole — up to the endpoint's own `MAX_LIMIT` (activity_service.py),
 * the ceiling the CSV download uses too. Past it the strip shows dashes rather than a
 * partial sum. Completeness is judged on the response (`summarizeActivity`), never on this
 * number, so a server that lowered its ceiling costs the strip its figures, not its truth.
 */
const SUMMARY_LIMIT = 500

/** The ranges that make sense for a ledger: a fixed window, not a chart's zoom. */
const RANGES: TimeRange[] = ['1M', '3M', '6M', 'YTD', '1Y', '2Y', 'ALL']

const KIND_LABELS: Record<ActivityKind, string> = {
  trade: 'Trades',
  dividend: 'Dividends',
  cash_flow: 'Cash',
  corporate_action: 'Corporate actions',
}

const KIND_ICONS: Record<ActivityKind, LucideIcon> = {
  trade: ArrowLeftRight,
  dividend: HandCoins,
  cash_flow: Banknote,
  corporate_action: Split,
}

const ALL_KINDS = Object.keys(KIND_LABELS) as ActivityKind[]

/** Money in is gain ink, money out loss ink: the sign is the direction of the cash. */
function signTone(amount: number | null | undefined): Tone {
  if (amount == null || amount === 0) return 'muted'
  return amount > 0 ? 'positive' : 'negative'
}

function signClass(amount: number | null | undefined): string {
  return TONE_TEXT[signTone(amount)]
}

function num(value: number | null, digits = 2): string {
  if (value == null) return ABSENT
  return value.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

/**
 * A share count, with as much precision as it actually carries and no more.
 *
 * Rounding to whole shares turned this account's fractional trades into `0` and, worse,
 * `-0` — 0.3 MU and 0.1 CSU are ordinary rows here, not edge cases. Six decimals is the
 * column's own precision (`Numeric(18, 6)`); `maximumFractionDigits` alone trims the
 * trailing zeros, so a whole-share trade still reads `50` rather than `50.000000`.
 */
function qty(value: number | null): string {
  if (value == null) return ABSENT
  // Intl renders -0.0000001 as "-0"; the sign is already in the BUY/SELL badge.
  const shown = Object.is(value, -0) ? 0 : value
  return shown.toLocaleString('en-US', { maximumFractionDigits: 6 })
}

interface ActivityBadge {
  text: string
  tone: Tone
  icon: LucideIcon
}

/**
 * What a row's type badge says, its glyph, and its tint.
 *
 * The transfer case is the one that earns its colour: an incoming transfer booked as
 * an ordinary deposit shows up as a portfolio-sized fake contribution, and until this
 * tab existed the only way to eyeball those rows was `manage_cash_flows list` over ssh.
 * Hence amber for Transfer against blue for Deposit and Withdrawal; `e2e/ledger.mjs`
 * reads the word, and the icon is decoration beside it.
 */
function activityBadge(row: ActivityRow): ActivityBadge {
  if (row.kind === 'cash_flow') {
    if (row.counts_as_money_in) {
      return (row.amount_base ?? 0) < 0
        ? { text: 'Withdrawal', tone: 'neutral', icon: ArrowUpFromLine }
        : { text: 'Deposit', tone: 'neutral', icon: ArrowDownToLine }
    }
    return { text: 'Transfer', tone: 'warning', icon: ArrowRightLeft }
  }
  if (row.kind === 'trade') {
    if (row.is_cancellation) {
      return isSell(row)
        ? { text: 'Sell · cancelled', tone: 'muted', icon: Minus }
        : { text: 'Buy · cancelled', tone: 'muted', icon: Plus }
    }
    if (isSell(row)) return { text: 'Sell', tone: 'negative', icon: Minus }
    return row.subtype
      ? { text: 'Buy', tone: 'positive', icon: Plus }
      : { text: 'Trade', tone: 'neutral', icon: ArrowLeftRight }
  }
  if (row.kind === 'dividend') {
    // Estimates are a gross guess with no withholding — the same honesty flag the tax
    // report and the dividend card carry.
    return row.source === 'yfinance_estimate'
      ? { text: 'Dividend · est.', tone: 'muted', icon: HandCoins }
      : { text: 'Dividend', tone: 'positive', icon: HandCoins }
  }
  return { text: sentenceCase(row.subtype) || 'Action', tone: 'muted', icon: Split }
}

/** `FORWARDSPLIT` → `Forwardsplit`; `CASH RECEIPTS / X` → `Cash receipts / x`. */
function sentenceCase(text: string | null | undefined): string {
  if (!text) return ''
  if (/[a-z]/.test(text)) return text
  const lower = text.toLowerCase()
  return lower.charAt(0).toUpperCase() + lower.slice(1)
}

/**
 * The row's broker text, where it says something the other columns do not: a corporate
 * action's terms, or what a non-contribution transfer was. A trade's "BUY 0.6 CRDO" and a
 * deposit's "CASH RECEIPTS / ELECTRONIC FUND TRANSFERS" only repeat the type and amount.
 */
function detailOf(row: ActivityRow): string | null {
  if (row.kind === 'trade' || row.kind === 'dividend') return null
  if (row.kind === 'cash_flow' && row.counts_as_money_in) return null
  return sentenceCase(row.description) || null
}

/** The security's logo or monogram; a cash flow, which has no security, the cash chip. */
function activityAvatar(row: ActivityRow, className: string) {
  if (row.symbol) {
    return <TickerAvatar symbol={row.symbol} securityId={row.security_id ?? undefined} className={className} />
  }
  return (
    <span
      aria-hidden="true"
      className={cn('inline-flex shrink-0 items-center justify-center rounded-full', TONE_CHIP.neutral, className)}
    >
      <Banknote className="h-1/2 w-1/2" />
    </span>
  )
}

/**
 * The ledger's eight columns, described once for both renderings.
 *
 * Note the title: a cash flow has no symbol at all, so it falls back to its own badge
 * text ("Deposit", "Transfer") rather than leaving the card headless.
 * The desktop table can print an em dash in a cell; a card cannot.
 */
function activityColumns(deps: {
  /** The response's own base currency: the amounts were converted into it. */
  baseCurrency: string
}): Column<ActivityRow>[] {
  const { baseCurrency } = deps
  // Through `formatCurrency`, so a debit reads "-CHF 163.58" like every other figure in the
  // app; the hand-built `${symbol}${number}` printed "CHF -163.58".
  const money = (n: number) => formatCurrency(n, baseCurrency)
  return [
    {
      key: 'symbol',
      header: 'Asset',
      shortHeader: 'Asset',
      mobile: 'title',
      avatar: activityAvatar,
      cell: (row, view) => {
        const detail = detailOf(row)
        // The type badge sits under the title on a card, so a cash row is titled "Cash"
        // rather than repeating "Deposit" twice.
        if (view !== 'table') return row.symbol ?? 'Cash'
        return (
          <div className="flex items-center gap-3">
            {activityAvatar(row, 'h-8 w-8 text-[10px]')}
            <div className="min-w-0">
              <div className="font-medium">{row.symbol ?? 'Cash'}</div>
              {detail && (
                <div className="max-w-[22rem] truncate text-xs text-muted-foreground" title={row.description}>
                  {detail}
                </div>
              )}
            </div>
          </div>
        )
      },
    },
    {
      key: 'date',
      header: 'Date',
      shortHeader: 'Date',
      mobile: 'meta',
      cellClassName: 'whitespace-nowrap',
      cell: (row) => formatDate(row.date),
    },
    {
      key: 'description',
      header: 'Detail',
      shortHeader: 'Detail',
      mobile: 'meta',
      // The desktop name cell carries it as a sub-line.
      desktop: 'hide',
      cell: (row) => detailOf(row),
    },
    {
      key: 'kind',
      header: 'Type',
      shortHeader: 'Type',
      mobile: 'badge',
      cell: (row) => {
        const badge = activityBadge(row)
        const Icon = badge.icon
        return (
          <span
            className={cn(
              'inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium',
              TONE_CHIP[badge.tone],
            )}
          >
            <Icon aria-hidden="true" className="h-3 w-3 shrink-0" strokeWidth={2.5} />
            {badge.text}
          </span>
        )
      },
    },
    {
      key: 'amount',
      header: 'Amount',
      shortHeader: 'Amount',
      align: 'right',
      mobile: 'value',
      tone: (row) => signClass(row.amount_base),
      cellClassName: 'font-medium',
      cell: (row) => (row.amount_base == null ? ABSENT : money(row.amount_base)),
    },
    {
      key: 'realized',
      header: 'Realized',
      shortHeader: 'Realized',
      align: 'right',
      mobile: 'delta',
      tone: (row) => signClass(row.realized_pnl_base),
      // A table cell cannot be empty, so the desktop keeps its em dash. In the card's
      // delta slot a lone dash reads as an error rather than as "not applicable".
      cell: (row, view) =>
        row.realized_pnl_base == null
          ? view === 'table' ? ABSENT : null
          : money(row.realized_pnl_base),
    },
    {
      key: 'quantity',
      header: 'Shares',
      shortHeader: 'Shares',
      align: 'right',
      cell: (row) => qty(row.quantity),
    },
    {
      key: 'price',
      header: 'Price',
      shortHeader: 'Price',
      align: 'right',
      cell: (row) =>
        row.price == null ? ABSENT : `${num(row.price)} ${row.currency ?? ''}`.trim(),
    },
  ]
}

/**
 * What the filtered window adds up to — figures only, each `null` (a dash) unless every row
 * behind it is in hand (`summarizeActivity`). Signed like the Amount column: money out of
 * the account is negative and loss-coloured.
 */
function ActivityFigures({ summary, currency }: { summary: ActivitySummary; currency: string }) {
  const money = (value: number | null) =>
    value == null ? null : formatSigned(value, (n) => formatCurrency(n, currency))
  const tone = (value: number | null): Tone => (value == null ? 'neutral' : signTone(value))
  return (
    <KpiPanel columns={6}>
      <KpiCard tile label="Events" icon={Hash} value={formatCount(summary.events)} />
      <KpiCard tile label="Bought" icon={ShoppingCart} iconTone="neutral"
        value={money(summary.bought)} tone={tone(summary.bought)} />
      <KpiCard tile label="Sold" icon={Tag} iconTone="neutral"
        value={money(summary.sold)} tone={tone(summary.sold)} />
      <KpiCard tile label="Realized" icon={CircleDollarSign}
        value={money(summary.realized)} tone={tone(summary.realized)} />
      <KpiCard tile label="Dividends" icon={HandCoins}
        value={summary.dividends == null ? null : (
          <>
            {money(summary.dividends)}
            {summary.dividendsEstimated && (
              <span className="ml-1 text-xs font-normal text-muted-foreground">est.</span>
            )}
          </>
        )}
        tone={tone(summary.dividends)} />
      <KpiCard tile label="Net deposits" icon={Wallet} iconTone="neutral"
        value={money(summary.netDeposits)} tone={tone(summary.netDeposits)} />
    </KpiPanel>
  )
}

/**
 * The account's transaction ledger.
 *
 * `trades`, `corporate_actions`, `cash_flows` and `dividend_payments` were all ingested,
 * reconciled and depended on — the tax report reads trades, the contributions splice
 * reads cash flows — while none of them had any read surface at all.
 */
export function ActivityTab() {
  const { baseCurrency } = useBaseCurrency()
  const [range, setRange] = useState<TimeRange>('1Y')
  const [kinds, setKinds] = useState<ActivityKind[]>([])
  const [symbol, setSymbol] = useState('')
  const [symbolDraft, setSymbolDraft] = useState('')
  const [page, setPage] = useState(0)

  // Shares Dashboard's inception source so ALL starts at the first tax lot rather than
  // a hardcoded date — transferred lots keep their original open_date.
  const { data: contributions } = useQuery(contributionsQuery)

  const window = useMemo(
    () => rangeFor(range, new Date(), contributions?.first_contribution_date ?? null),
    [range, contributions]
  )

  const params = useMemo(
    () => ({
      startDate: window.start,
      endDate: window.end,
      kinds: kinds.length ? kinds : undefined,
      symbol: symbol || undefined,
      limit: PAGE_SIZE,
      offset: page * PAGE_SIZE,
    }),
    [window, kinds, symbol, page]
  )

  const keepPrevious = useKeepPreviousData()
  const { data, isLoading, isError, isPlaceholderData } = useQuery({
    ...activityQuery(params),
    // The last page stays while the next one loads (#68): Previous/Next sit under the table,
    // so blanking it unmounted the button just pressed. Only across a page turn: the figures
    // strip sums the rows against the kind filter, and old rows under a new filter would be
    // wrong figures, not merely stale ones.
    placeholderData: (previous, previousQuery) =>
      isPageTurn(previousQuery?.queryKey[2], params) ? keepPrevious(previous) : undefined,
  })

  /** Any filter change invalidates the page number — page 4 of a narrower result is
      usually empty, which reads as "no activity" rather than "you moved the goalposts". */
  const withReset = <T,>(setter: (v: T) => void) => (value: T) => {
    setter(value)
    setPage(0)
  }

  const toggleKind = withReset((kind: ActivityKind) =>
    setKinds(current =>
      current.includes(kind) ? current.filter(k => k !== kind) : [...current, kind]
    )
  )

  const columns = useMemo(
    () => activityColumns({ baseCurrency: data?.base_currency || baseCurrency }),
    [baseCurrency, data?.base_currency]
  )

  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.limit)) : 1

  // The whole window, for the figures strip — only when one page does not already hold it
  // and the endpoint's ceiling would (see SUMMARY_LIMIT). Pure DB, like the page itself.
  const wholeParams = useMemo(
    () => ({ ...params, limit: SUMMARY_LIMIT, offset: 0 }),
    [params]
  )
  const pageIsWhole = !!data && data.offset === 0 && data.items.length === data.total
  const { data: whole } = useQuery({
    ...activityQuery(wholeParams),
    enabled: !!data && !pageIsWhole && data.total <= SUMMARY_LIMIT,
  })
  const summary = useMemo(() => {
    if (!data) return null
    // A paged `data` sums to dashes by itself; `whole` must describe the same window.
    return summarizeActivity(!pageIsWhole && whole?.total === data.total ? whole : data, kinds)
  }, [data, whole, pageIsWhole, kinds])

  return (
    <div className="space-y-6">
      <Card>
        <SectionHeader
          title="Activity"
          icon={ArrowLeftRight}
          description={data ? `${formatDate(data.start_date)} – ${formatDate(data.end_date)}` : undefined}
          aside={
            <a
              href={api.getActivityCsvUrl({ ...params, limit: undefined, offset: undefined })}
              className="inline-flex h-9 items-center justify-center gap-2 whitespace-nowrap rounded-lg border border-input bg-card px-3 shadow-sm text-sm font-medium ring-offset-background transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
            >
              <Download className="h-4 w-4" />
              Download CSV
            </a>
          }
        />

        <div className="px-[var(--card-padding)] pb-[var(--card-padding)]">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-3">
            {/* Scrolls below `sm` rather than wrapping: seven range buttons come to
                ~384px against ~308px of card interior at 390px, so wrapping cost a
                second row — and the kind group below cost two more. The negative
                margin plus matching padding let the row reach the card edge, so the
                next pill is visibly half-cut instead of the group looking complete. */}
            <SegmentedControl
              label="Time range"
              variant="buttons"
              value={range}
              onChange={r => withReset(setRange)(r)}
              className="-mx-1 flex snap-x snap-mandatory gap-1 overflow-x-auto px-1 sm:flex-wrap sm:overflow-visible"
              buttonClassName="shrink-0 snap-start"
              options={RANGES.map(r => ({ value: r, label: r }))}
            />

            {/* Empty selection means everything, so nothing is highlighted and no
                combination of clicks can produce an empty table by accident. */}
            <SegmentedControl
              label="Event types"
              variant="buttons"
              isSelected={kind => kinds.includes(kind)}
              onToggle={kind => toggleKind(kind)}
              className="-mx-1 flex snap-x snap-mandatory gap-1 overflow-x-auto px-1 sm:flex-wrap sm:overflow-visible"
              buttonClassName="shrink-0 snap-start px-2.5 sm:px-3"
              options={ALL_KINDS.map(kind => {
                const Icon = KIND_ICONS[kind]
                return {
                  value: kind,
                  label: (
                    <>
                      {/* Icon-only below `sm`: "Corporate actions" and its three siblings
                          come to ~430px of labels, and the icons are already the thing
                          that distinguishes them. The label survives for screen readers,
                          so this sheds pixels rather than meaning — the same trade
                          DividendYearComparison makes for its bar at this width. */}
                      <Icon className="h-4 w-4 sm:mr-1.5 sm:h-3.5 sm:w-3.5" aria-hidden="true" />
                      <span className="hidden sm:inline">{KIND_LABELS[kind]}</span>
                      <span className="sr-only sm:hidden">{KIND_LABELS[kind]}</span>
                    </>
                  ),
                }
              })}
            />

            <form
              onSubmit={e => {
                e.preventDefault()
                withReset(setSymbol)(symbolDraft.trim().toUpperCase())
              }}
              className="flex flex-wrap items-center gap-2"
            >
              <label htmlFor="activity-symbol" className="sr-only">
                Filter by ticker
              </label>
              <input
                id="activity-symbol"
                value={symbolDraft}
                onChange={e => setSymbolDraft(e.target.value)}
                placeholder="Ticker"
                className="h-9 w-28 rounded-lg border border-input bg-card px-3 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
              />
              <Button type="submit" size="sm" variant="outline">
                Filter
              </Button>
              {symbol && (
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    setSymbolDraft('')
                    withReset(setSymbol)('')
                  }}
                >
                  Clear
                </Button>
              )}
            </form>
          </div>
        </div>

        <CardContent className="space-y-4" aria-busy={isPlaceholderData || undefined}>
          {summary && !isError && (
            <ActivityFigures summary={summary} currency={data?.base_currency || baseCurrency} />
          )}
          {isLoading ? (
            <div className="h-[400px] w-full animate-pulse rounded-md bg-muted" />
          ) : isError ? (
            // Never let a backend failure read as "nothing happened in this window".
            <p className="py-12 text-center text-muted-foreground">
              Couldn't load activity — the server didn't respond. It retries automatically.
            </p>
          ) : !data || data.items.length === 0 ? (
            <p className="py-12 text-center text-muted-foreground">
              No activity in this window.
              {(kinds.length > 0 || symbol) && ' Try widening the filters.'}
            </p>
          ) : (
            <>
              <DataTable
                rows={data.items}
                columns={columns}
                // Rows without an `ib_key` — every estimated dividend — cluster on
                // dates, and `kind-date` alone collided on them (two estimates paid the
                // same day were one React key). The symbol and the position in the page
                // make the fallback unique within a render.
                getRowKey={(row, index) =>
                  row.ib_key ?? `${row.kind}-${row.date}-${row.symbol ?? ''}-${index}`
                }
                label="Activity table"
                className={placeholderDim(isPlaceholderData)}
                density="normal"
                caption={`Account activity from ${data.start_date} to ${data.end_date}, newest first`}
              />

              {/* Wraps: "1–100 of 12,345" plus Previous/Next is ~300px against ~308px
                  of card interior, so a long total is one digit from clipping. */}
              {totalPages > 1 && (
                <div className="mt-4 flex flex-wrap items-center justify-between gap-3 text-sm text-muted-foreground">
                  <span>
                    {data.offset + 1}–{Math.min(data.offset + data.limit, data.total)} of{' '}
                    {formatCount(data.total)}
                  </span>
                  <div className="flex gap-2">
                    <Button variant="outline" size="sm" disabled={page === 0} onClick={() => setPage(p => p - 1)}>
                      Previous
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={page >= totalPages - 1}
                      onClick={() => setPage(p => p + 1)}
                    >
                      Next
                    </Button>
                  </div>
                </div>
              )}

            </>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
