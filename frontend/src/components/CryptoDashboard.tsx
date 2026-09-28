import { lazy } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Clock, Download, RefreshCw } from 'lucide-react'
import { api, type CryptoStatusResponse, type CryptoSyncResponse } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { LazyTabPanel } from '@/components/ui/LazyTabPanel'
import { cryptoAccess, isAccessRefusal, retryUnlessRefused, type CryptoAccess } from '@/lib/cryptoAccess'
import { formatCount, formatShortDateTime } from '@/lib/utils'
import { AppHeader } from './AppHeader'
import { AppFooter } from './AppFooter'

/**
 * The page is the lazy half: Recharts is already in the shared chunk (docs/tech-stack.md),
 * but the crypto view's own code is fetched only the first time someone switches to it,
 * and behind `LazyTabPanel`'s boundary a failure there leaves the header — and the way
 * back to stocks — working.
 */
const CryptoOverview = lazy(() =>
  import('./CryptoOverview').then(m => ({ default: m.CryptoOverview })),
)

/**
 * The crypto mode's page: the shared header with the crypto sync's status and button,
 * then the overview (docs/crypto.md).
 */
export function CryptoDashboard() {
  const queryClient = useQueryClient()

  const { data: health } = useQuery({
    queryKey: ['health'],
    queryFn: () => api.healthCheck(),
    staleTime: Infinity,
  })
  const serverHasNoKey = health?.write_auth_enabled === false

  const statusQuery = useQuery({
    queryKey: ['crypto', 'status'],
    queryFn: () => api.getCryptoStatus(),
    enabled: !serverHasNoKey,
    retry: retryUnlessRefused,
    // Polling stops at a refusal: asking every minute with the same key cannot unlock it,
    // and saving a key refetches everything anyway (AdminKeyButton invalidates the cache).
    refetchInterval: query => (isAccessRefusal(query.state.error) ? false : 60_000),
  })
  const access = cryptoAccess(statusQuery.error, health?.write_auth_enabled)

  const syncMutation = useMutation({
    mutationFn: () => api.syncCrypto(),
    onSettled: () => {
      // A run that failed still records itself, so the status line has news either way.
      queryClient.invalidateQueries({ queryKey: ['crypto'] })
    },
  })

  const canSync = access === 'open' && statusQuery.data?.configured !== false

  return (
    <div className="min-h-screen bg-background">
      <AppHeader
        strapline="Your CoinStats portfolio — kept apart from the stock book"
        status={<CryptoStatusLine access={access} status={statusQuery.data} />}
        actions={
          <Button
            onClick={() => syncMutation.mutate()}
            disabled={syncMutation.isPending || !canSync}
            variant="outline"
            className="px-3 sm:px-4"
            aria-label={syncMutation.isPending ? 'Syncing crypto' : 'Sync crypto'}
            title={canSync ? 'Fetch the CoinStats portfolio now' : undefined}
          >
            {syncMutation.isPending ? (
              <RefreshCw className="h-4 w-4 animate-spin sm:mr-2" />
            ) : (
              <Download className="h-4 w-4 sm:mr-2" />
            )}
            <span className="hidden sm:inline">
              {syncMutation.isPending ? 'Syncing...' : 'Sync crypto'}
            </span>
          </Button>
        }
        below={
          <CryptoSyncMessage
            data={syncMutation.isSuccess ? syncMutation.data : undefined}
            error={syncMutation.isError ? syncMutation.error : null}
          />
        }
      />

      <div className="mx-auto w-full max-w-[1400px] px-4 py-4 sm:px-6 sm:py-6">
        <LazyTabPanel label="Crypto">
          <CryptoOverview serverHasNoKey={serverHasNoKey} />
        </LazyTabPanel>
      </div>

      <AppFooter />
    </div>
  )
}

/** The header's status line for the crypto book: last run, next run, credits. */
export function CryptoStatusLine({
  access, status,
}: { access: CryptoAccess; status: CryptoStatusResponse | undefined }) {
  const line = 'flex flex-wrap items-center gap-x-3 gap-y-1 mt-2 text-xs text-muted-foreground'
  if (access === 'locked') {
    return (
      <div className={line}>
        <Clock className="h-3 w-3 shrink-0" />
        <span>Locked — add the admin key to view crypto</span>
      </div>
    )
  }
  if (access === 'server-has-no-key' || !status) return null
  if (!status.configured) {
    return (
      <div className={line}>
        <Clock className="h-3 w-3 shrink-0" />
        <span>CoinStats is not configured</span>
      </div>
    )
  }

  const run = status.last_run
  return (
    <>
      <div className={line}>
        <Clock className="h-3 w-3 shrink-0" />
        {run?.finished_at ? (
          <span>Last sync: {formatShortDateTime(run.finished_at)} ({run.status})</span>
        ) : (
          <span>No crypto sync has run yet</span>
        )}
        {status.next_run && <span>· Next: {formatShortDateTime(status.next_run)}</span>}
        {status.credits_remaining != null && status.credits_total != null && (
          <span>
            · CoinStats credits: {formatCount(status.credits_remaining)} of{' '}
            {formatCount(status.credits_total)} left
          </span>
        )}
      </div>
      {/* A bare "(error)" isn't actionable — show what actually went wrong. */}
      {run && run.status !== 'success' && run.message && (
        <p className="mt-1 max-w-3xl text-xs text-amber-700 dark:text-amber-400">{run.message}</p>
      )}
    </>
  )
}

/** What the header says after "Sync crypto" is pressed. */
export function CryptoSyncMessage({
  data, error,
}: { data?: CryptoSyncResponse; error?: { message: string } | null }) {
  if (error) {
    return (
      <div className="mt-4 rounded-lg border border-red-200 bg-red-50 p-4 dark:border-red-800 dark:bg-red-950">
        <p className="text-sm text-red-800 dark:text-red-200">✗ Crypto sync failed: {error.message}</p>
      </div>
    )
  }
  if (!data) return null
  if (data.status !== 'success') {
    const failed = data.status === 'error'
    return (
      <div className={failed
        ? 'mt-4 rounded-lg border border-red-200 bg-red-50 p-4 dark:border-red-800 dark:bg-red-950'
        : 'mt-4 rounded-lg border bg-muted p-4'}
      >
        <p className={failed ? 'text-sm text-red-800 dark:text-red-200' : 'text-sm text-muted-foreground'}>
          {failed ? '✗' : '⏸'} {data.message}
        </p>
      </div>
    )
  }
  return (
    <div className="mt-4 rounded-lg border border-green-200 bg-green-50 p-4 dark:border-green-800 dark:bg-green-950">
      <p className="text-sm text-green-800 dark:text-green-200">✓ {data.message}</p>
      {data.warnings.map((warning, i) => (
        <p key={i} className="mt-1 text-sm text-yellow-800 dark:text-yellow-200">⚠ {warning}</p>
      ))}
    </div>
  )
}
