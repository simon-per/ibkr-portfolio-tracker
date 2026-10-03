import { useMemo, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type CryptoHistoryPoint, type CryptoPortfolioResponse } from '@/lib/api'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { DataTable } from '@/components/ui/DataTable'
import type { SortDirection } from '@/components/ui/SortableTh'
import { KpiCard, KpiCardSkeleton, KpiPanel, type KpiTone } from '@/components/ui/KpiCard'
import { DeltaChip } from './DeltaChip'
import { CryptoHistoryChart } from './CryptoHistoryChart'
import { CryptoAllocationChart } from './CryptoAllocationChart'
import { cryptoHoldingColumns, sortCryptoHoldings, type CryptoSortColumn } from './cryptoColumns'
import { useBaseCurrency, useFormatCurrency } from '@/lib/CurrencyContext'
import { cryptoAccess, retryUnlessRefused } from '@/lib/cryptoAccess'
import { formatCount, formatCurrency as formatIn, formatDate, formatPrice, formatShortDateTime } from '@/lib/utils'

/**
 * The crypto book (docs/crypto.md): one page, no tab strip — the section strip is the
 * stock view's, and the e2e suite holds the page to exactly one.
 *
 * Every figure arrives in the base currency from `/api/crypto/*`: CoinStats' holdings at
 * CoinGecko's prices, computed by the backend; nothing is recomputed here. What this page
 * adds is the reading of it — and that means the qualifiers sit on the surface beside the
 * figures they qualify: when the snapshot was taken, which days of the P&L are
 * reconstructed, which coins could not be priced, and what sits beside the total.
 */

function Notice({
  title, children, tone = 'neutral',
}: { title: string; children: ReactNode; tone?: 'neutral' | 'error' }) {
  return (
    <Card role={tone === 'error' ? 'alert' : undefined}>
      <CardHeader>
        <CardTitle className={tone === 'error' ? 'text-red-700 dark:text-red-400' : undefined}>
          {title}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2 text-sm text-muted-foreground">{children}</CardContent>
    </Card>
  )
}

const signTone = (value: number | null | undefined): KpiTone =>
  value == null ? 'neutral' : value >= 0 ? 'positive' : 'negative'

export function CryptoOverview({ serverHasNoKey }: { serverHasNoKey: boolean }) {
  const portfolioQuery = useQuery({
    queryKey: ['crypto', 'portfolio'],
    queryFn: () => api.getCryptoPortfolio(),
    enabled: !serverHasNoKey,
    retry: retryUnlessRefused,
  })
  const hasSnapshot = !!portfolioQuery.data?.as_of
  const historyQuery = useQuery({
    queryKey: ['crypto', 'history'],
    queryFn: () => api.getCryptoHistory(),
    enabled: !serverHasNoKey && hasSnapshot,
    retry: retryUnlessRefused,
  })

  const access = cryptoAccess(portfolioQuery.error, serverHasNoKey ? false : undefined)
  if (access === 'server-has-no-key') {
    return (
      <Notice title="Crypto needs an admin key on the server">
        <p>
          The crypto book is private: its API refuses every request until the server has an
          admin key, so no key in this browser can open it. Set <code>API_ADMIN_TOKEN</code> in{' '}
          <code>backend/.env</code> and recreate the container.
        </p>
        <p>The stock view is unaffected.</p>
      </Notice>
    )
  }
  if (access === 'locked') {
    return (
      <Notice title="Crypto is locked">
        <p>
          Crypto balances are private. Add the admin key with the lock button at the top
          right to view them — it is kept in this browser only.
        </p>
      </Notice>
    )
  }
  if (portfolioQuery.isError) {
    return (
      <Notice title="Couldn't load the crypto portfolio" tone="error">
        <p>
          {portfolioQuery.error instanceof Error && portfolioQuery.error.message
            ? portfolioQuery.error.message
            : 'The backend did not respond.'}{' '}
          It is asked again on the next visit.
        </p>
      </Notice>
    )
  }
  if (!portfolioQuery.data) {
    return (
      <KpiPanel columns={6}>
        <KpiCardSkeleton count={5} tile />
      </KpiPanel>
    )
  }

  const portfolio = portfolioQuery.data
  if (!portfolio.as_of) {
    return portfolio.configured ? (
      <Notice title="No crypto sync has run yet">
        <p>
          Press <span className="font-medium text-foreground">Sync crypto</span> above to fetch
          your CoinStats portfolio. After that it refreshes at the scheduled sync times.
        </p>
      </Notice>
    ) : (
      <Notice title="CoinStats is not configured">
        <p>
          Set <code>COIN_STATS_API_KEY</code> and <code>COIN_STATS_SHARE_TOKEN</code> in{' '}
          <code>backend/.env</code> — the share token is the part after <code>/p/</code> in a
          CoinStats portfolio share link — then recreate the container.
        </p>
      </Notice>
    )
  }

  return (
    <CryptoBook
      portfolio={portfolio}
      history={historyQuery.data?.points ?? []}
      historyWarnings={historyQuery.data?.warnings ?? []}
      historyLoading={historyQuery.isLoading}
      historyFailed={historyQuery.isError}
    />
  )
}

function CryptoBook({
  portfolio, history, historyWarnings, historyLoading, historyFailed,
}: {
  portfolio: CryptoPortfolioResponse
  history: CryptoHistoryPoint[]
  historyWarnings: string[]
  historyLoading: boolean
  historyFailed: boolean
}) {
  const { baseCurrency } = useBaseCurrency()
  const formatCurrency = useFormatCurrency()
  const money = (value: number | null | undefined) => (value == null ? null : formatCurrency(value))
  const signedMoney = (value: number | null | undefined) =>
    value == null ? null : `${value > 0 ? '+' : ''}${formatCurrency(value)}`

  const [sortColumn, setSortColumn] = useState<CryptoSortColumn | null>('value')
  const [sortDirection, setSortDirection] = useState<SortDirection>('desc')
  const handleSort = (column: CryptoSortColumn) => {
    if (sortColumn === column) {
      setSortDirection(d => (d === 'asc' ? 'desc' : 'asc'))
    } else {
      setSortColumn(column)
      setSortDirection(column === 'symbol' ? 'asc' : 'desc')
    }
  }
  const rows = useMemo(
    () => sortCryptoHoldings(portfolio.holdings, sortColumn, sortDirection),
    [portfolio.holdings, sortColumn, sortDirection],
  )
  const columns = useMemo(
    () => cryptoHoldingColumns({
      formatCurrency: (value: number) => formatIn(value, baseCurrency),
      formatPrice: (value: number) => formatPrice(value, baseCurrency),
    }),
    [baseCurrency],
  )

  // The two endpoints name the same unknown day the same way; say it once.
  const warnings = [...new Set([...portfolio.warnings, ...historyWarnings])]
  const plural = (n: number, word: string) => `${formatCount(n)} ${word}${n === 1 ? '' : 's'}`
  const startLabel = formatDate(portfolio.start_date)

  return (
    <div className="space-y-6 sm:space-y-8">
      {!portfolio.configured && (
        <p className="rounded-md border border-yellow-600/40 bg-yellow-600/10 px-3 py-2 text-xs text-yellow-700 dark:text-yellow-500" role="alert">
          CoinStats is no longer configured on the server. These are the last figures it
          reported and they will not update.
        </p>
      )}
      {warnings.length > 0 && (
        <div className="rounded-md border border-yellow-600/40 bg-yellow-600/10 px-3 py-2 text-xs text-yellow-700 dark:text-yellow-500" role="alert">
          <ul className="list-disc space-y-0.5 pl-4">
            {warnings.map((warning, i) => <li key={i}>{warning}</li>)}
          </ul>
        </div>
      )}

      <section aria-label="Crypto totals" className="space-y-2">
        <KpiPanel columns={4}>
          <KpiCard
            tile
            hero
            label="Total value"
            value={money(portfolio.total_value)}
            sub={portfolio.no_price_symbols.length === 0
              ? undefined
              : portfolio.total_value == null
                ? `Unknown: no price for ${portfolio.no_price_symbols.join(', ')}`
                // Left out, never valued at 0 — and said here, beside the figure.
                : `Excludes ${portfolio.no_price_symbols.join(', ')} — no price`}
          />
          <KpiCard
            tile
            label="Today"
            value={signedMoney(portfolio.change_today)}
            tone={signTone(portfolio.change_today)}
            footer={<DeltaChip pct={portfolio.change_today_pct} label="since 00:00 UTC" flatBand={0} />}
          />
          <KpiCard
            tile
            label={`P&L since ${startLabel}`}
            value={signedMoney(portfolio.pnl_since_start)}
            tone={signTone(portfolio.pnl_since_start)}
            sub="Price moves only"
          />
          <KpiCard
            tile
            label="Coins"
            value={formatCount(portfolio.valued_count)}
            sub={portfolio.unpriced_count > 0 ? `${plural(portfolio.unpriced_count, 'unpriced coin')} left out` : undefined}
          />
        </KpiPanel>

        {/* The qualifiers, on the surface beside the figures they qualify. */}
        <div className="space-y-1 text-xs text-muted-foreground">
          <p>
            As of {portfolio.as_of ? formatShortDateTime(portfolio.as_of) : '—'}
            {portfolio.cash_value
              ? ` · exchange cash ${formatCurrency(portfolio.cash_value)} not included`
              : ''}
            {portfolio.defi_value
              ? ` · DeFi ${formatCurrency(portfolio.defi_value)} not included`
              : ''}
          </p>
          {portfolio.peg_note && (
            <p className="text-amber-700 dark:text-amber-400">{portfolio.peg_note}</p>
          )}
          {portfolio.unpriced_count > 0 && (
            <p className="text-amber-700 dark:text-amber-400">
              {plural(portfolio.unpriced_count, 'coin')} CoinStats cannot price, left out of every
              total rather than valued at zero: {portfolio.unpriced_symbols.join(', ')}
            </p>
          )}
          {portfolio.spam_count > 0 && (
            <p>{plural(portfolio.spam_count, 'spam token')} hidden.</p>
          )}
        </div>
      </section>

      <div className="grid gap-6 lg:grid-cols-3 [&>*]:min-w-0">
        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>Crypto over time</CardTitle>
          </CardHeader>
          <CardContent>
            {historyFailed ? (
              <p className="py-8 text-center text-sm text-muted-foreground">
                Couldn&apos;t load the history — the backend didn&apos;t respond.
              </p>
            ) : historyLoading ? (
              <div className="h-[260px] animate-pulse rounded-md bg-muted sm:h-[340px]" />
            ) : (
              <CryptoHistoryChart
                points={history}
                startDate={portfolio.start_date}
                basketDate={portfolio.basket_date}
                firstSnapshotDate={portfolio.first_snapshot_date}
              />
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Allocation</CardTitle>
            {portfolio.no_price_symbols.length > 0 && (
              <CardDescription>Priced coins only</CardDescription>
            )}
          </CardHeader>
          <CardContent>
            <CryptoAllocationChart portfolio={portfolio} />
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Holdings</CardTitle>
          <CardDescription>
            {plural(portfolio.valued_count, 'coin')}
            {portfolio.unpriced_count > 0 ? `, ${plural(portfolio.unpriced_count, 'unpriced')}` : ''}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <DataTable
            rows={rows}
            columns={columns}
            getRowKey={h => h.coin_id}
            label="Crypto holdings"
            caption="Crypto holdings from CoinStats at CoinGecko prices"
            sort={{ column: sortColumn, direction: sortDirection, onSort: handleSort }}
            minWidthClassName="min-w-[760px]"
            footer={[
              { key: 'total-label', spans: ['coin', 'quantity', 'price'], content: 'Total' },
              {
                key: 'total-value',
                spans: ['value'],
                align: 'right',
                content: money(portfolio.total_value) ?? '—',
              },
            ]}
          />
        </CardContent>
      </Card>
    </div>
  )
}

